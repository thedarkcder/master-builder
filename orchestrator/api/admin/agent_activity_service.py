from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import desc, select

from orchestrator.api.schemas import AgentActivityRead, AgentEventRead
from orchestrator.storage.models import AgentLifecycleEvent


def _coerce_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def list_agent_activity(
    *,
    session,
    tenant_id: str | None = None,
    project_id: str | None = None,
    heartbeat_timeout_seconds: int = 300,
) -> list[AgentActivityRead]:
    tenant_filter = str(tenant_id or "").strip() or None
    project_filter = str(project_id or "").strip() or None
    timeout = max(1, heartbeat_timeout_seconds)
    now = datetime.now(timezone.utc)

    query = select(AgentLifecycleEvent).order_by(
        desc(AgentLifecycleEvent.recorded_at),
        desc(AgentLifecycleEvent.event_id),
    )
    if tenant_filter is not None:
        query = query.where(AgentLifecycleEvent.tenant_id == tenant_filter)
    if project_filter is not None:
        query = query.where(AgentLifecycleEvent.project_id == project_filter)
    events = session.execute(query.limit(10000)).scalars().all()

    grouped: dict[tuple[str, str], list[AgentEventRead]] = {}
    heartbeats: dict[tuple[str, str], datetime] = {}
    for event in events:
        key = (event.tenant_id, event.agent_id)
        if key not in heartbeats:
            heartbeats[key] = _coerce_aware(event.recorded_at)
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
        event_list.sort(key=lambda entry: entry.recorded_at, reverse=True)
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
                events=list(reversed(event_list[:25])),
            )
        )

    activity.sort(key=lambda entry: entry.last_seen_at, reverse=True)
    return activity
