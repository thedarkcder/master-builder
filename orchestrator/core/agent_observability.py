from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4
import threading

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from orchestrator.core.log_event_bus import EVENT_KIND_AGENT_LIFECYCLE, register_stream_offsets
from orchestrator.storage.models import AgentLifecycleEvent, RunStreamEvent

ALLOWED_AGENT_EVENTS = {
    "ISSUE_ASSIGNED",
    "TASK_STARTED",
    "TASK_COMPLETED",
    "TASK_FAILED",
    "PULL_FROM_STAGE",
    "MERGE_TO_STAGE",
    "BUILD_STARTED",
    "BUILD_FAILED",
    "TEST_FAILED",
    "LOCK_ACQUIRED",
    "PLAN_POSTED",
    "PR_OPENED",
    "RUN_FAILED",
}
MAX_IN_MEMORY_EVENT_HISTORY = 1000
MAX_PERSISTED_EVENTS_PER_TENANT = 5000


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_event_type(event_type: str) -> str:
    normalized = str(event_type or "").strip().upper()
    if normalized in ALLOWED_AGENT_EVENTS:
        return normalized
    return "TASK_FAILED"


@dataclass
class AgentEventRecord:
    event_type: str
    tenant_id: str
    project_id: str | None
    run_id: str
    issue_key: str | None
    agent_id: str
    recorded_at: datetime


class AgentObservabilityTracker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: list[AgentEventRecord] = []
        self._heartbeats: dict[tuple[str, str], datetime] = {}

    def reset(self) -> None:
        with self._lock:
            self._events.clear()
            self._heartbeats.clear()

    def record_event(
        self,
        *,
        event_type: str,
        tenant_id: str,
        project_id: str | None,
        run_id: str,
        issue_key: str | None,
        agent_id: str,
        recorded_at: datetime | None = None,
    ) -> None:
        timestamp = recorded_at or _utcnow()
        event = AgentEventRecord(
            event_type=_normalize_event_type(event_type),
            tenant_id=str(tenant_id or "").strip(),
            project_id=str(project_id or "").strip() or None,
            run_id=str(run_id or "").strip(),
            issue_key=str(issue_key or "").strip() or None,
            agent_id=str(agent_id or "").strip() or "unknown-agent",
            recorded_at=timestamp,
        )
        with self._lock:
            self._events.append(event)
            if len(self._events) > MAX_IN_MEMORY_EVENT_HISTORY:
                overflow = len(self._events) - MAX_IN_MEMORY_EVENT_HISTORY
                del self._events[:overflow]
            self._heartbeats[(event.tenant_id, event.agent_id)] = timestamp

    def snapshot(self) -> tuple[list[AgentEventRecord], dict[tuple[str, str], datetime]]:
        with self._lock:
            return list(self._events), dict(self._heartbeats)


def record_agent_lifecycle_event(
    *,
    session: Session,
    event_type: str,
    tenant_id: str,
    project_id: str | None,
    run_id: str,
    issue_key: str | None,
    agent_id: str,
    recorded_at: datetime | None = None,
    max_events_per_tenant: int = MAX_PERSISTED_EVENTS_PER_TENANT,
) -> None:
    timestamp = recorded_at or _utcnow()
    normalized_tenant = str(tenant_id or "").strip()
    if not normalized_tenant:
        return
    normalized_project = str(project_id or "").strip() or None
    normalized_run = str(run_id or "").strip()
    if not normalized_run:
        return
    normalized_agent = str(agent_id or "").strip() or "unknown-agent"
    normalized_event_type = _normalize_event_type(event_type)

    agent_observability_tracker.record_event(
        event_type=normalized_event_type,
        tenant_id=normalized_tenant,
        project_id=normalized_project,
        run_id=normalized_run,
        issue_key=str(issue_key or "").strip() or None,
        agent_id=normalized_agent,
        recorded_at=timestamp,
    )

    session.add(
        AgentLifecycleEvent(
            event_id=uuid4().hex,
            tenant_id=normalized_tenant,
            project_id=normalized_project,
            run_id=normalized_run,
            issue_key=str(issue_key or "").strip() or None,
            agent_id=normalized_agent,
            event_type=normalized_event_type,
            recorded_at=timestamp,
        )
    )
    stream_row = RunStreamEvent(
        event_kind=EVENT_KIND_AGENT_LIFECYCLE,
        tenant_id=normalized_tenant,
        project_id=normalized_project,
        run_id=normalized_run,
        issue_key=str(issue_key or "").strip() or None,
        agent_id=normalized_agent,
        event_type=normalized_event_type,
        invocation_id=None,
        channel=None,
        command=None,
        working_dir=None,
        stage=None,
        attempt=None,
        stream=None,
        message=None,
        recorded_at=timestamp,
    )
    session.add(stream_row)
    session.flush()
    register_stream_offsets(session=session, rows=[stream_row])


agent_observability_tracker = AgentObservabilityTracker()


def reset_agent_observability_for_tests() -> None:
    agent_observability_tracker.reset()


def prune_agent_lifecycle_events(
    *,
    session: Session,
    max_events_per_tenant: int = MAX_PERSISTED_EVENTS_PER_TENANT,
) -> int:
    ranked_legacy = (
        select(
            AgentLifecycleEvent.event_id,
            func.row_number()
            .over(
                partition_by=AgentLifecycleEvent.tenant_id,
                order_by=(AgentLifecycleEvent.recorded_at.desc(), AgentLifecycleEvent.event_id.desc()),
            )
            .label("row_number"),
        ).subquery()
    )
    cutoff_legacy_ids = session.execute(
        select(ranked_legacy.c.event_id).where(ranked_legacy.c.row_number > max(0, int(max_events_per_tenant)))
    ).scalars().all()
    deleted = 0
    if cutoff_legacy_ids:
        result = session.execute(delete(AgentLifecycleEvent).where(AgentLifecycleEvent.event_id.in_(cutoff_legacy_ids)))
        deleted += int(result.rowcount or 0)

    ranked_stream = (
        select(
            RunStreamEvent.stream_offset,
            func.row_number()
            .over(
                partition_by=RunStreamEvent.tenant_id,
                order_by=RunStreamEvent.stream_offset.desc(),
            )
            .label("row_number"),
        )
        .where(RunStreamEvent.event_kind == EVENT_KIND_AGENT_LIFECYCLE)
        .subquery()
    )
    cutoff_stream_ids = session.execute(
        select(ranked_stream.c.stream_offset).where(ranked_stream.c.row_number > max(0, int(max_events_per_tenant)))
    ).scalars().all()
    if cutoff_stream_ids:
        result = session.execute(delete(RunStreamEvent).where(RunStreamEvent.stream_offset.in_(cutoff_stream_ids)))
        deleted += int(result.rowcount or 0)
    return deleted
