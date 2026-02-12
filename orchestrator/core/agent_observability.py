from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import threading

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
}


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
            self._heartbeats[(event.tenant_id, event.agent_id)] = timestamp

    def snapshot(self) -> tuple[list[AgentEventRecord], dict[tuple[str, str], datetime]]:
        with self._lock:
            return list(self._events), dict(self._heartbeats)


agent_observability_tracker = AgentObservabilityTracker()


def reset_agent_observability_for_tests() -> None:
    agent_observability_tracker.reset()

