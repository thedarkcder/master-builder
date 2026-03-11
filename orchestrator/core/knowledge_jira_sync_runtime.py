from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from sqlalchemy import select

from orchestrator.api.admin.route_helpers import jira_oauth_client, refresh_jira_connection_tokens
from orchestrator.core.config import Settings, get_settings
from orchestrator.core.knowledge_base import sync_project_knowledge_from_jira
from orchestrator.core.project_policy import resolve_effective_policy
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import JiraOAuthConnection, Project, Tenant
from orchestrator.storage.run_queue_events import is_postgres_database_url, postgres_dsn_from_database_url

try:
    import psycopg
except ImportError:  # pragma: no cover - dependency is required at runtime
    psycopg = None


logger = logging.getLogger("orchestrator.knowledge_jira_sync_runtime")


@dataclass(frozen=True)
class _SyncProject:
    tenant_id: str
    project_id: str
    jira_project_key: str
    connection_id: str


class KnowledgeJiraSyncDependencyFailure(RuntimeError):
    pass


def _try_acquire_leader_lock(*, conn, lock_key: int) -> bool:  # noqa: ANN001
    with conn.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", (lock_key,))
        row = cursor.fetchone()
    return bool(row and row[0] is True)


def _leader_lock_healthcheck(*, conn) -> bool:  # noqa: ANN001
    with conn.cursor() as cursor:
        cursor.execute("SELECT 1")
        row = cursor.fetchone()
    return bool(row and row[0] == 1)


class KnowledgeJiraSyncRuntime:
    def __init__(self, *, settings: Settings) -> None:
        self._settings = settings
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not bool(getattr(self._settings, "knowledge_jira_auto_sync_enabled", True)):
            logger.info("knowledge_jira_sync_runtime_disabled")
            return
        if not is_postgres_database_url(self._settings.database_url):
            logger.info("knowledge_jira_sync_runtime_skipped_non_postgres")
            return
        if psycopg is None:
            raise KnowledgeJiraSyncDependencyFailure(
                "Knowledge Jira sync runtime requires psycopg to coordinate leader lock."
            )
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="knowledge-jira-sync-runtime", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self) -> None:
        lock_key = int(getattr(self._settings, "knowledge_jira_sync_lock_key", 947102033128))
        poll_seconds = max(5, int(getattr(self._settings, "knowledge_jira_sync_poll_seconds", 30)))
        interval_seconds = max(60, int(getattr(self._settings, "knowledge_jira_sync_interval_seconds", 3600)))
        dsn = postgres_dsn_from_database_url(self._settings.database_url)
        logger.info(
            "knowledge_jira_sync_runtime_started lock_key=%s poll_seconds=%s interval_seconds=%s",
            lock_key,
            poll_seconds,
            interval_seconds,
        )
        while not self._stop_event.is_set():
            try:
                with psycopg.connect(dsn, autocommit=True) as conn:
                    acquired = _try_acquire_leader_lock(conn=conn, lock_key=lock_key)
                    if not acquired:
                        self._stop_event.wait(timeout=poll_seconds)
                        continue
                    logger.info("knowledge_jira_sync_leader_acquired lock_key=%s", lock_key)
                    try:
                        while not self._stop_event.is_set():
                            self._run_sync_pass()
                            waited = 0
                            while waited < interval_seconds and not self._stop_event.is_set():
                                _leader_lock_healthcheck(conn=conn)
                                step = min(poll_seconds, interval_seconds - waited)
                                self._stop_event.wait(timeout=step)
                                waited += step
                    finally:
                        logger.info("knowledge_jira_sync_leader_released lock_key=%s", lock_key)
            except Exception as exc:  # noqa: BLE001
                logger.exception("knowledge_jira_sync_runtime_loop_failed error=%s", exc)
                self._stop_event.wait(timeout=poll_seconds)
        logger.info("knowledge_jira_sync_runtime_stopped")

    def _run_sync_pass(self) -> None:
        session_factory = create_session_factory(self._settings.database_url)
        with session_factory() as session:
            projects = self._list_sync_projects(session)
        if not projects:
            logger.debug("knowledge_jira_sync_no_projects")
            return

        for project in projects:
            if self._stop_event.is_set():
                break
            try:
                with session_factory() as session:
                    connection = session.get(JiraOAuthConnection, project.connection_id)
                    if connection is None:
                        logger.warning(
                            "knowledge_jira_sync_missing_connection tenant_id=%s project_id=%s connection_id=%s",
                            project.tenant_id,
                            project.project_id,
                            project.connection_id,
                        )
                        continue
                    access_token = refresh_jira_connection_tokens(
                        session,
                        connection=connection,
                        settings=self._settings,
                        tenant_id=project.tenant_id,
                    )
                    client = jira_oauth_client(
                        session=session,
                        settings=self._settings,
                        tenant_id=project.tenant_id,
                        project_id=project.project_id,
                    )
                    result = sync_project_knowledge_from_jira(
                        session=session,
                        tenant_id=project.tenant_id,
                        project_id=project.project_id,
                        project_key=project.jira_project_key,
                        jira_client=client,
                        access_token=access_token,
                        cloud_id=connection.cloud_id,
                        max_issues=max(1, int(getattr(self._settings, "knowledge_jira_sync_max_issues", 500))),
                    )
                    logger.info(
                        "knowledge_jira_sync_project_complete tenant_id=%s project_id=%s jira_project_key=%s created=%s updated=%s unchanged=%s deleted=%s skipped=%s failed=%s",
                        project.tenant_id,
                        project.project_id,
                        project.jira_project_key,
                        result.created_assets,
                        result.updated_assets,
                        result.unchanged_assets,
                        result.deleted_assets,
                        result.skipped_assets,
                        result.failed_assets,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "knowledge_jira_sync_project_failed tenant_id=%s project_id=%s jira_project_key=%s error=%s",
                    project.tenant_id,
                    project.project_id,
                    project.jira_project_key,
                    exc,
                )

    def _list_sync_projects(self, session) -> list[_SyncProject]:  # noqa: ANN001
        tenants = {
            tenant.tenant_id: tenant
            for tenant in session.execute(select(Tenant).where(Tenant.is_enabled.is_(True))).scalars().all()
        }
        if not tenants:
            return []
        projects: list[_SyncProject] = []
        all_projects = session.execute(select(Project).where(Project.is_archived.is_(False))).scalars().all()
        for project in all_projects:
            tenant = tenants.get(project.tenant_id)
            if tenant is None:
                continue
            connection_id = str((tenant.jira_config or {}).get("connection_id") or "").strip()
            if not connection_id:
                continue
            jira_project_key = str(project.jira_project_key or "").strip().upper()
            if not jira_project_key:
                continue
            effective_policy = resolve_effective_policy(
                tenant_policy=dict(tenant.policy_config or {}),
                project_overrides=dict(project.policy_overrides or {}),
                default_codex_model=getattr(self._settings, "codex_model", None),
                default_codex_reasoning_effort=getattr(self._settings, "codex_reasoning_effort", None),
            )
            if not bool(effective_policy.get("knowledge_base_enabled", True)):
                continue
            projects.append(
                _SyncProject(
                    tenant_id=project.tenant_id,
                    project_id=project.project_id,
                    jira_project_key=jira_project_key,
                    connection_id=connection_id,
                )
            )
        return projects


def build_knowledge_jira_sync_runtime(*, settings: Settings | None = None) -> KnowledgeJiraSyncRuntime:
    return KnowledgeJiraSyncRuntime(settings=settings or get_settings())
