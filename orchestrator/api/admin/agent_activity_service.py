from __future__ import annotations

from datetime import datetime, timedelta, timezone

from orchestrator.api.schemas import AgentActivityRead, AgentEventRead
from orchestrator.core.agent_observability import agent_observability_tracker


def _coerce_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def list_agent_activity(
    *,
    tenant_id: str | None = None,
    project_id: str | None = None,
    heartbeat_timeout_seconds: int = 300,
) -> list[AgentActivityRead]:
    events, heartbeats = agent_observability_tracker.snapshot()
    tenant_filter = str(tenant_id or "").strip() or None
    project_filter = str(project_id or "").strip() or None
    timeout = max(1, heartbeat_timeout_seconds)
    now = datetime.now(timezone.utc)

    grouped: dict[tuple[str, str], list[AgentEventRead]] = {}
    for event in events:
        if tenant_filter is not None and event.tenant_id != tenant_filter:
            continue
        if project_filter is not None and event.project_id != project_filter:
            continue
        key = (event.tenant_id, event.agent_id)
        grouped.setdefault(key, []).append(
            AgentEventRead(
                event_type=event.event_type,
                run_id=event.run_id,
                issue_key=event.issue_key,
                project_id=event.project_id,
                recorded_at=_coerce_aware(event.recorded_at),
            )
        )

    activity: list[AgentActivityRead] = []
    for (tenant_value, agent_id), event_list in grouped.items():
        event_list.sort(key=lambda entry: entry.recorded_at)
        last_seen = heartbeats.get((tenant_value, agent_id))
        if last_seen is None:
            continue
        last_seen_aware = _coerce_aware(last_seen)
        is_dark = (now - last_seen_aware) > timedelta(seconds=timeout)
        activity.append(
            AgentActivityRead(
                tenant_id=tenant_value,
                agent_id=agent_id,
                last_seen_at=last_seen_aware,
                is_dark=is_dark,
                events=event_list[-25:],
            )
        )

    activity.sort(key=lambda entry: entry.last_seen_at, reverse=True)
    return activity

