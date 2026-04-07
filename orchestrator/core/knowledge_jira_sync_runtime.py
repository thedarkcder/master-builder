from __future__ import annotations

import logging
import os
import signal
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from orchestrator.api.jira_oauth.service import execute_jira_operation_with_refresh_retry
from orchestrator.core.config import Settings, get_settings
from orchestrator.core.knowledge_base import sync_project_knowledge_from_jira
from orchestrator.core.knowledge_jira_sync_status import (
    KnowledgeJiraSyncRuntimeStatus,
    get_runtime_status,
    upsert_project_status,
    upsert_runtime_status,
)
from orchestrator.core.logging import configure_logging
from orchestrator.core.project_policy import resolve_effective_policy
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import JiraOAuthConnection, KnowledgeJiraSyncProjectState, KnowledgeSource, Project, Tenant
from orchestrator.storage.run_queue_events import is_postgres_database_url, postgres_dsn_from_database_url
from orchestrator.tools.jira_oauth_models import JiraOAuthAuthRequiredError, JiraOAuthError, JiraOAuthHttpError

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


def _service_instance_id() -> str:
    return f"{os.uname().nodename}:{os.getpid()}"


def run_knowledge_jira_sync() -> None:
    settings = get_settings()
    configure_logging(
        settings.log_level,
        environment=settings.sentry_environment,
        platform_version=settings.sentry_release or "dev-local",
        default_agent_id="knowledge-jira-sync",
    )
    KnowledgeJiraSyncRuntime(settings=settings).run_forever()


class KnowledgeJiraSyncRuntime:
    def __init__(self, *, settings: Settings) -> None:
        self._settings = settings
        self._session_factory = create_session_factory(settings.database_url)
        self._service_instance_id = _service_instance_id()

    def run_forever(self) -> None:
        if not bool(getattr(self._settings, "knowledge_jira_auto_sync_enabled", True)):
            self._write_runtime_status(state="disabled", leader_acquired=False)
            logger.info("knowledge_jira_sync_runtime_disabled")
            return
        if not is_postgres_database_url(self._settings.database_url):
            raise KnowledgeJiraSyncDependencyFailure(
                "Knowledge Jira sync runtime requires PostgreSQL advisory locks; "
                "set ORCHESTRATOR_DATABASE_URL to a postgresql URL."
            )
        if psycopg is None:
            raise KnowledgeJiraSyncDependencyFailure(
                "Knowledge Jira sync runtime requires psycopg to coordinate leader lock."
            )

        stop_event = threading.Event()
        lock_key = int(getattr(self._settings, "knowledge_jira_sync_lock_key", 947102033128))
        poll_seconds = max(5, int(getattr(self._settings, "knowledge_jira_sync_poll_seconds", 30)))
        interval_seconds = max(60, int(getattr(self._settings, "knowledge_jira_sync_interval_seconds", 3600)))
        dsn = postgres_dsn_from_database_url(self._settings.database_url)
        started_at = datetime.now(timezone.utc)
        self._write_runtime_status(
            state="starting",
            started_at=started_at,
            stopped_at=None,
            leader_acquired=False,
        )

        def _request_stop() -> None:
            stop_event.set()

        signal.signal(signal.SIGINT, lambda _sig, _frame: _request_stop())
        signal.signal(signal.SIGTERM, lambda _sig, _frame: _request_stop())

        logger.info(
            "knowledge_jira_sync_runtime_started lock_key=%s poll_seconds=%s interval_seconds=%s",
            lock_key,
            poll_seconds,
            interval_seconds,
        )
        while not stop_event.is_set():
            try:
                with psycopg.connect(dsn, autocommit=True) as conn:
                    acquired = _try_acquire_leader_lock(conn=conn, lock_key=lock_key)
                    if not acquired:
                        self._write_runtime_status(state="running", leader_acquired=False)
                        stop_event.wait(timeout=poll_seconds)
                        continue

                    logger.info("knowledge_jira_sync_leader_acquired lock_key=%s", lock_key)
                    self._write_runtime_status(state="running", leader_acquired=True)
                    try:
                        while not stop_event.is_set():
                            self._run_sync_pass()
                            waited = 0
                            while waited < interval_seconds and not stop_event.is_set():
                                _leader_lock_healthcheck(conn=conn)
                                self._write_runtime_status(state=self._current_runtime_state(), leader_acquired=True)
                                step = min(poll_seconds, interval_seconds - waited)
                                stop_event.wait(timeout=step)
                                waited += step
                    finally:
                        self._write_runtime_status(state=self._current_runtime_state(), leader_acquired=False)
                        logger.info("knowledge_jira_sync_leader_released lock_key=%s", lock_key)
            except Exception as exc:  # noqa: BLE001
                self._write_runtime_status(state="degraded", leader_acquired=False)
                logger.exception("knowledge_jira_sync_runtime_loop_failed error=%s", exc)
                stop_event.wait(timeout=poll_seconds)

        self._write_runtime_status(
            state="stopped",
            stopped_at=datetime.now(timezone.utc),
            leader_acquired=False,
        )
        logger.info("knowledge_jira_sync_runtime_stopped")

    def _run_sync_pass(self) -> None:
        pass_started_at = datetime.now(timezone.utc)
        self._write_runtime_status(
            state=self._current_runtime_state(default="running"),
            last_pass_started_at=pass_started_at,
            leader_acquired=True,
        )
        with self._session_factory() as session:
            projects = self._list_sync_projects(session)
            existing_status = {
                (status.tenant_id, status.project_id): status
                for status in get_runtime_status(session=session, settings=self._settings).projects
            }

        if not projects:
            logger.debug("knowledge_jira_sync_no_projects")
            self._write_runtime_status(
                state="running",
                last_pass_finished_at=datetime.now(timezone.utc),
                leader_acquired=True,
            )
            return

        any_degraded = False
        active_projects = {(project.tenant_id, project.project_id) for project in projects}
        for project in projects:
            now = datetime.now(timezone.utc)
            existing_project_status = existing_status.get((project.tenant_id, project.project_id))
            if (
                existing_project_status is not None
                and existing_project_status.failure_category in {"invalid_refresh_token", "auth_required"}
                and existing_project_status.next_retry_at is not None
                and existing_project_status.next_retry_at > now
            ):
                logger.info(
                    "knowledge_jira_sync_project_skipped tenant_id=%s project_id=%s jira_project_key=%s reason=%s_backoff next_retry_at=%s",
                    project.tenant_id,
                    project.project_id,
                    project.jira_project_key,
                    existing_project_status.failure_category,
                    existing_project_status.next_retry_at.isoformat(),
                )
                continue
            try:
                result = self._sync_project(project=project)
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
                self._write_project_status(
                    project=project,
                    state="healthy",
                    failure_category=None,
                    last_error=None,
                    last_attempted_at=now,
                    last_successful_sync_at=now,
                    next_retry_at=None,
                    consecutive_failures=0,
                )
            except Exception as exc:  # noqa: BLE001
                category = _classify_project_failure(exc)
                any_degraded = True
                next_retry_at = (
                    now
                    + timedelta(
                        seconds=max(
                            60,
                            int(getattr(self._settings, "knowledge_jira_sync_invalid_token_backoff_seconds", 21600)),
                        )
                    )
                    if category in {"invalid_refresh_token", "auth_required"}
                    else None
                )
                self._write_project_status(
                    project=project,
                    state="degraded",
                    failure_category=category,
                    last_error=str(exc),
                    last_attempted_at=now,
                    last_successful_sync_at=existing_project_status.last_successful_sync_at if existing_project_status else None,
                    next_retry_at=next_retry_at,
                    consecutive_failures=(existing_project_status.consecutive_failures + 1) if existing_project_status else 1,
                )
                if category in {"invalid_refresh_token", "auth_required"}:
                    logger.error(
                        "knowledge_jira_sync_project_degraded tenant_id=%s project_id=%s jira_project_key=%s category=%s next_retry_at=%s error=%s",
                        project.tenant_id,
                        project.project_id,
                        project.jira_project_key,
                        category,
                        next_retry_at.isoformat() if next_retry_at is not None else None,
                        exc,
                    )
                else:
                    logger.exception(
                        "knowledge_jira_sync_project_failed tenant_id=%s project_id=%s jira_project_key=%s category=%s error=%s",
                        project.tenant_id,
                        project.project_id,
                        project.jira_project_key,
                        category,
                        exc,
                    )
        self._prune_inactive_project_statuses(active_projects=active_projects)
        self._write_runtime_status(
            state="degraded" if any_degraded else "running",
            last_pass_finished_at=datetime.now(timezone.utc),
            leader_acquired=True,
        )

    def _sync_project(self, *, project: _SyncProject):
        with self._session_factory() as session:
            connection = session.get(JiraOAuthConnection, project.connection_id)
            if connection is None:
                raise KnowledgeJiraSyncDependencyFailure("Jira OAuth connection record not found.")
            cloud_id = connection.cloud_id

        def _operation(session: Session, jira_client, access_token: str):  # noqa: ANN001
            return sync_project_knowledge_from_jira(
                session=session,
                tenant_id=project.tenant_id,
                project_id=project.project_id,
                project_key=project.jira_project_key,
                jira_client=jira_client,
                access_token=access_token,
                cloud_id=cloud_id,
                max_issues=max(1, int(getattr(self._settings, "knowledge_jira_sync_max_issues", 500))),
            )

        return execute_jira_operation_with_refresh_retry(
            session_factory=self._session_factory,
            settings=self._settings,
            connection_id=project.connection_id,
            operation=_operation,
            tenant_id=project.tenant_id,
            project_id=project.project_id,
        )

    def _list_sync_projects(self, session: Session) -> list[_SyncProject]:
        tenants = {
            tenant.tenant_id: tenant
            for tenant in session.execute(select(Tenant).where(Tenant.is_enabled.is_(True))).scalars().all()
        }
        if not tenants:
            return []
        projects: list[_SyncProject] = []
        all_projects = session.execute(select(Project).where(Project.is_archived.is_(False))).scalars().all()
        jira_sources = list(
            session.execute(
                select(KnowledgeSource).where(
                    KnowledgeSource.connector_type == "jira",
                    KnowledgeSource.status == "active",
                    KnowledgeSource.sync_mode == "scheduled",
                )
            ).scalars()
        )
        jira_sources_by_project: dict[str, list[KnowledgeSource]] = {}
        for source in jira_sources:
            jira_sources_by_project.setdefault(source.project_id, []).append(source)

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
            configured_sources = jira_sources_by_project.get(project.project_id, [])
            if configured_sources:
                for source in configured_sources:
                    source_project_key = str((source.config_json or {}).get("project_key") or jira_project_key).strip().upper()
                    if not source_project_key:
                        continue
                    projects.append(
                        _SyncProject(
                            tenant_id=project.tenant_id,
                            project_id=project.project_id,
                            jira_project_key=source_project_key,
                            connection_id=connection_id,
                        )
                    )
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

    def _current_runtime_state(self, *, default: str = "running") -> str:
        with self._session_factory() as session:
            return get_runtime_status(session=session, settings=self._settings).state or default

    def _write_runtime_status(
        self,
        *,
        state: str,
        started_at: datetime | None = None,
        stopped_at: datetime | None = None,
        last_pass_started_at: datetime | None = None,
        last_pass_finished_at: datetime | None = None,
        leader_acquired: bool | None = None,
    ) -> None:
        with self._session_factory() as session:
            upsert_runtime_status(
                session=session,
                settings=self._settings,
                state=state,
                started_at=started_at,
                stopped_at=stopped_at,
                last_pass_started_at=last_pass_started_at,
                last_pass_finished_at=last_pass_finished_at,
                last_heartbeat_at=datetime.now(timezone.utc),
                leader_acquired=leader_acquired,
                service_instance_id=self._service_instance_id,
            )
            session.commit()

    def _write_project_status(
        self,
        *,
        project: _SyncProject,
        state: str,
        failure_category: str | None,
        last_error: str | None,
        last_attempted_at: datetime | None,
        last_successful_sync_at: datetime | None,
        next_retry_at: datetime | None,
        consecutive_failures: int,
    ) -> None:
        with self._session_factory() as session:
            upsert_project_status(
                session=session,
                tenant_id=project.tenant_id,
                project_id=project.project_id,
                jira_project_key=project.jira_project_key,
                state=state,
                failure_category=failure_category,
                last_error=last_error,
                last_attempted_at=last_attempted_at,
                last_successful_sync_at=last_successful_sync_at,
                next_retry_at=next_retry_at,
                consecutive_failures=consecutive_failures,
            )
            session.commit()

    def _prune_inactive_project_statuses(self, *, active_projects: set[tuple[str, str]]) -> None:
        with self._session_factory() as session:
            runtime_status = get_runtime_status(session=session, settings=self._settings)
            active_rows = {
                (project_status.tenant_id, project_status.project_id): project_status
                for project_status in runtime_status.projects
            }
            stale_projects = set(active_rows).difference(active_projects)
            if stale_projects:
                from orchestrator.core.knowledge_jira_sync_status import RUNTIME_NAME
                session.query(KnowledgeJiraSyncProjectState).filter(
                    KnowledgeJiraSyncProjectState.runtime_name == RUNTIME_NAME,
                    tuple_(KnowledgeJiraSyncProjectState.tenant_id, KnowledgeJiraSyncProjectState.project_id).in_(
                        list(stale_projects)
                    ),
                ).delete(synchronize_session=False)
                session.commit()


def _classify_project_failure(exc: Exception) -> str:
    if isinstance(exc, JiraOAuthAuthRequiredError):
        return "auth_required"
    if isinstance(exc, JiraOAuthHttpError) and exc.status_code in {401, 403}:
        return "auth_required"
    if isinstance(exc, JiraOAuthError):
        message = str(exc).lower()
        if "refresh_token is invalid" in message or "unauthorized_client" in message:
            return "invalid_refresh_token"
        if "(401)" in message or "(403)" in message:
            return "auth_required"
        return "sync_request_failed"
    if isinstance(exc, KnowledgeJiraSyncDependencyFailure):
        return "dependency_failure"
    return "unknown_error"


def get_knowledge_jira_sync_runtime_status(
    *,
    session: Session | None = None,
    settings: Settings | None = None,
) -> KnowledgeJiraSyncRuntimeStatus:
    resolved_settings = settings or get_settings()
    if session is not None:
        return get_runtime_status(session=session, settings=resolved_settings)
    session_factory = create_session_factory(resolved_settings.database_url)
    with session_factory() as managed_session:
        return get_runtime_status(session=managed_session, settings=resolved_settings)
