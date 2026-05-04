from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4
import threading

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from orchestrator.core.observability.audit import record_audit_event
from orchestrator.core.observability.logging_pane import emit_agent_lifecycle_log_event
from orchestrator.storage.models import AgentLifecycleEvent, Run

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
    workflow_id = None
    run = session.get(Run, normalized_run)
    if run is not None:
        workflow_id = run.workflow_id

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
    record_audit_event(
        session,
        tenant_id=normalized_tenant,
        project_id=normalized_project,
        workflow_id=workflow_id,
        run_id=normalized_run,
        operation_id=None,
        attempt_id=None,
        issue_key=str(issue_key or "").strip() or None,
        actor_type="agent",
        actor_id=normalized_agent,
        source_component="agent_observability",
        event_kind=normalized_event_type.lower(),
        level="info",
        message=f"Agent lifecycle event: {normalized_event_type}",
        payload={"event_type": normalized_event_type},
        recorded_at=timestamp,
    )
    emit_agent_lifecycle_log_event(
        session=session,
        tenant_id=normalized_tenant,
        project_id=normalized_project,
        workflow_id=workflow_id,
        run_id=normalized_run,
        issue_key=str(issue_key or "").strip() or None,
        agent_id=normalized_agent,
        event_type=normalized_event_type,
        recorded_at=timestamp,
    )
    session.flush()


agent_observability_tracker = AgentObservabilityTracker()


def reset_agent_observability_for_tests() -> None:
    agent_observability_tracker.reset()


def prune_agent_lifecycle_events(
    *,
    session: Session,
    max_events_per_tenant: int = MAX_PERSISTED_EVENTS_PER_TENANT,
) -> int:
    ranked_events = (
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
    cutoff_event_ids = session.execute(
        select(ranked_events.c.event_id).where(ranked_events.c.row_number > max(0, int(max_events_per_tenant)))
    ).scalars().all()
    deleted = 0
    if cutoff_event_ids:
        result = session.execute(delete(AgentLifecycleEvent).where(AgentLifecycleEvent.event_id.in_(cutoff_event_ids)))
        deleted += int(result.rowcount or 0)

    return deleted
