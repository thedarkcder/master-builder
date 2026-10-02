from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.core.config import Settings
from orchestrator.storage.models import (
    KnowledgeJiraSyncProjectState,
    KnowledgeJiraSyncRuntimeState,
)
from orchestrator.storage.run_queue_events import is_postgres_database_url

RUNTIME_NAME = "knowledge-jira-sync"


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class KnowledgeJiraSyncProjectStatus:
    tenant_id: str
    project_id: str
    jira_project_key: str
    state: str
    failure_category: str | None
    last_error: str | None
    last_attempted_at: datetime | None
    last_successful_sync_at: datetime | None
    next_retry_at: datetime | None
    consecutive_failures: int


@dataclass(frozen=True)
class KnowledgeJiraSyncRuntimeStatus:
    state: str
    enabled: bool
    database_backend: str
    started_at: datetime | None
    stopped_at: datetime | None
    last_pass_started_at: datetime | None
    last_pass_finished_at: datetime | None
    last_heartbeat_at: datetime | None
    leader_acquired: bool
    service_instance_id: str | None
    stale: bool
    projects: tuple[KnowledgeJiraSyncProjectStatus, ...]


def _database_backend(settings: Settings) -> str:
    if is_postgres_database_url(settings.database_url):
        return "postgres"
    if str(settings.database_url).startswith("sqlite:"):
        return "sqlite"
    return "other"


def _stale_cutoff(settings: Settings) -> timedelta:
    interval_seconds = max(
        60, int(getattr(settings, "knowledge_jira_sync_interval_seconds", 3600))
    )
    poll_seconds = max(
        5, int(getattr(settings, "knowledge_jira_sync_poll_seconds", 30))
    )
    return timedelta(seconds=max(interval_seconds * 2, poll_seconds * 4, 300))


def get_runtime_status(
    *, session: Session, settings: Settings
) -> KnowledgeJiraSyncRuntimeStatus:
    runtime_row = session.get(KnowledgeJiraSyncRuntimeState, RUNTIME_NAME)
    project_rows = (
        session.execute(
            select(KnowledgeJiraSyncProjectState)
            .where(KnowledgeJiraSyncProjectState.runtime_name == RUNTIME_NAME)
            .order_by(
                KnowledgeJiraSyncProjectState.tenant_id.asc(),
                KnowledgeJiraSyncProjectState.project_id.asc(),
                KnowledgeJiraSyncProjectState.jira_project_key.asc(),
            )
        )
        .scalars()
        .all()
    )

    projects = tuple(
        KnowledgeJiraSyncProjectStatus(
            tenant_id=row.tenant_id,
            project_id=row.project_id,
            jira_project_key=row.jira_project_key,
            state=row.state,
            failure_category=row.failure_category,
            last_error=row.last_error,
            last_attempted_at=_utc(row.last_attempted_at),
            last_successful_sync_at=_utc(row.last_successful_sync_at),
            next_retry_at=_utc(row.next_retry_at),
            consecutive_failures=int(row.consecutive_failures or 0),
        )
        for row in project_rows
    )

    enabled = bool(getattr(settings, "knowledge_jira_auto_sync_enabled", True))
    database_backend = _database_backend(settings)
    if runtime_row is None:
        return KnowledgeJiraSyncRuntimeStatus(
            state="disabled" if not enabled else "not_started",
            enabled=enabled,
            database_backend=database_backend,
            started_at=None,
            stopped_at=None,
            last_pass_started_at=None,
            last_pass_finished_at=None,
            last_heartbeat_at=None,
            leader_acquired=False,
            service_instance_id=None,
            stale=False,
            projects=projects,
        )

    now = datetime.now(timezone.utc)
    last_heartbeat_at = _utc(runtime_row.last_heartbeat_at)
    stale = (
        last_heartbeat_at is not None
        and runtime_row.state not in {"disabled", "stopped", "skipped_non_postgres"}
        and (now - last_heartbeat_at) > _stale_cutoff(settings)
    )
    state = "stale" if stale else runtime_row.state
    return KnowledgeJiraSyncRuntimeStatus(
        state=state,
        enabled=runtime_row.enabled,
        database_backend=runtime_row.database_backend,
        started_at=_utc(runtime_row.started_at),
        stopped_at=_utc(runtime_row.stopped_at),
        last_pass_started_at=_utc(runtime_row.last_pass_started_at),
        last_pass_finished_at=_utc(runtime_row.last_pass_finished_at),
        last_heartbeat_at=last_heartbeat_at,
        leader_acquired=runtime_row.leader_acquired,
        service_instance_id=runtime_row.service_instance_id,
        stale=stale,
        projects=projects,
    )


def upsert_runtime_status(
    *,
    session: Session,
    settings: Settings,
    state: str,
    started_at: datetime | None = None,
    stopped_at: datetime | None = None,
    last_pass_started_at: datetime | None = None,
    last_pass_finished_at: datetime | None = None,
    last_heartbeat_at: datetime | None = None,
    leader_acquired: bool | None = None,
    service_instance_id: str | None = None,
) -> KnowledgeJiraSyncRuntimeState:
    row = session.get(KnowledgeJiraSyncRuntimeState, RUNTIME_NAME)
    now = datetime.now(timezone.utc)
    if row is None:
        row = KnowledgeJiraSyncRuntimeState(
            runtime_name=RUNTIME_NAME,
            state=state,
            enabled=bool(getattr(settings, "knowledge_jira_auto_sync_enabled", True)),
            database_backend=_database_backend(settings),
            started_at=started_at,
            stopped_at=stopped_at,
            last_pass_started_at=last_pass_started_at,
            last_pass_finished_at=last_pass_finished_at,
            last_heartbeat_at=last_heartbeat_at,
            leader_acquired=bool(leader_acquired)
            if leader_acquired is not None
            else False,
            service_instance_id=service_instance_id,
            updated_at=now,
        )
        session.add(row)
    else:
        row.state = state
        row.enabled = bool(getattr(settings, "knowledge_jira_auto_sync_enabled", True))
        row.database_backend = _database_backend(settings)
        if started_at is not None:
            row.started_at = started_at
        if stopped_at is not None:
            row.stopped_at = stopped_at
        if last_pass_started_at is not None:
            row.last_pass_started_at = last_pass_started_at
        if last_pass_finished_at is not None:
            row.last_pass_finished_at = last_pass_finished_at
        if last_heartbeat_at is not None:
            row.last_heartbeat_at = last_heartbeat_at
        if leader_acquired is not None:
            row.leader_acquired = leader_acquired
        if service_instance_id is not None or row.service_instance_id is None:
            row.service_instance_id = service_instance_id
        row.updated_at = now
    return row


def upsert_project_status(
    *,
    session: Session,
    tenant_id: str,
    project_id: str,
    jira_project_key: str,
    state: str,
    failure_category: str | None,
    last_error: str | None,
    last_attempted_at: datetime | None,
    last_successful_sync_at: datetime | None,
    next_retry_at: datetime | None,
    consecutive_failures: int,
) -> KnowledgeJiraSyncProjectState:
    row = session.execute(
        select(KnowledgeJiraSyncProjectState).where(
            KnowledgeJiraSyncProjectState.runtime_name == RUNTIME_NAME,
            KnowledgeJiraSyncProjectState.tenant_id == tenant_id,
            KnowledgeJiraSyncProjectState.project_id == project_id,
        )
    ).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if row is None:
        row = KnowledgeJiraSyncProjectState(
            runtime_name=RUNTIME_NAME,
            tenant_id=tenant_id,
            project_id=project_id,
            jira_project_key=jira_project_key,
            state=state,
            failure_category=failure_category,
            last_error=last_error,
            last_attempted_at=last_attempted_at,
            last_successful_sync_at=last_successful_sync_at,
            next_retry_at=next_retry_at,
            consecutive_failures=consecutive_failures,
            updated_at=now,
        )
        session.add(row)
    else:
        row.jira_project_key = jira_project_key
        row.state = state
        row.failure_category = failure_category
        row.last_error = last_error
        row.last_attempted_at = last_attempted_at
        row.last_successful_sync_at = last_successful_sync_at
        row.next_retry_at = next_retry_at
        row.consecutive_failures = consecutive_failures
        row.updated_at = now
    return row
