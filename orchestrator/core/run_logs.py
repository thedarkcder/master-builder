from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import delete, desc, select
from sqlalchemy.orm import Session

from orchestrator.storage.models import RunLogEvent
from orchestrator.storage.run_event_stream import notify_run_log_event

MAX_PERSISTED_LOG_EVENTS_PER_RUN = 5000
MAX_LOG_MESSAGE_CHARS = 2000


@dataclass(frozen=True)
class NormalizedRunLogEvent:
    tenant_id: str
    project_id: str | None
    run_id: str | None
    issue_key: str | None
    agent_id: str
    invocation_id: str
    channel: str
    command: str
    working_dir: str | None
    stage: str
    attempt: int | None
    stream: str
    message: str
    recorded_at: datetime


def _normalize_run_log_event(
    *,
    tenant_id: str,
    project_id: str | None,
    run_id: str | None,
    issue_key: str | None,
    agent_id: str,
    invocation_id: str | None,
    channel: str | None,
    command: str | None,
    working_dir: str | None,
    stage: str,
    attempt: int | None,
    stream: str,
    message: str,
    recorded_at: datetime | None = None,
) -> NormalizedRunLogEvent | None:
    normalized_tenant = str(tenant_id or "").strip()
    if not normalized_tenant:
        raise ValueError("Codex log event requires tenant_id")
    normalized_message = str(message or "").strip()
    if not normalized_message:
        return None
    normalized_message = normalized_message[:MAX_LOG_MESSAGE_CHARS]
    timestamp = recorded_at or datetime.now(timezone.utc)
    normalized_stream = str(stream or "").strip().lower() or "stdout"
    if normalized_stream not in {"stdout", "stderr", "system"}:
        normalized_stream = "stdout"
    normalized_stage = str(stage or "").strip().lower()
    normalized_agent = str(agent_id or "").strip()
    normalized_run_id = str(run_id or "").strip() or None
    normalized_invocation_id = str(invocation_id or "").strip() or None
    normalized_channel = str(channel or "").strip() or None
    normalized_command = str(command or "").strip() or None
    normalized_working_dir = str(working_dir or "").strip() or None
    if not normalized_stage:
        raise ValueError("Codex log event requires stage")
    if not normalized_agent:
        raise ValueError("Codex log event requires agent_id")
    if normalized_invocation_id is None:
        raise ValueError("Codex log event requires invocation_id")
    if normalized_channel is None:
        raise ValueError("Codex log event requires channel")
    if normalized_command is None:
        raise ValueError("Codex log event requires command")
    return NormalizedRunLogEvent(
        tenant_id=normalized_tenant,
        project_id=str(project_id or "").strip() or None,
        run_id=normalized_run_id,
        issue_key=str(issue_key or "").strip() or None,
        agent_id=normalized_agent,
        invocation_id=normalized_invocation_id,
        channel=normalized_channel,
        command=normalized_command,
        working_dir=normalized_working_dir,
        stage=normalized_stage,
        attempt=attempt,
        stream=normalized_stream,
        message=normalized_message,
        recorded_at=timestamp,
    )


def _build_run_log_model(event: NormalizedRunLogEvent) -> RunLogEvent:
    return RunLogEvent(
        event_id=uuid4().hex,
        tenant_id=event.tenant_id,
        project_id=event.project_id,
        run_id=event.run_id,
        issue_key=event.issue_key,
        agent_id=event.agent_id,
        invocation_id=event.invocation_id,
        channel=event.channel,
        command=event.command,
        working_dir=event.working_dir,
        stage=event.stage,
        attempt=event.attempt,
        stream=event.stream,
        message=event.message,
        recorded_at=event.recorded_at,
    )


def _prune_invocation_events(
    *,
    session: Session,
    invocation_id: str,
    max_events_per_run: int,
) -> None:
    cutoff_query = (
        select(RunLogEvent.event_id)
        .where(RunLogEvent.invocation_id == invocation_id)
        .order_by(desc(RunLogEvent.recorded_at), desc(RunLogEvent.event_id))
        .offset(max(0, max_events_per_run))
    )
    cutoff_ids = session.execute(cutoff_query).scalars().all()
    if cutoff_ids:
        session.execute(delete(RunLogEvent).where(RunLogEvent.event_id.in_(cutoff_ids)))


def _notify_normalized_run_log_event(*, session: Session, event: NormalizedRunLogEvent) -> None:
    notify_run_log_event(
        session,
        tenant_id=event.tenant_id,
        run_id=event.run_id,
        issue_key=event.issue_key,
        project_id=event.project_id,
        agent_id=event.agent_id,
        invocation_id=event.invocation_id,
        channel=event.channel,
        command=event.command,
        working_dir=event.working_dir,
        stage=event.stage,
        attempt=event.attempt,
        stream=event.stream,
        message=event.message,
        recorded_at=event.recorded_at.isoformat(),
    )


def record_run_log_event(
    *,
    session: Session,
    tenant_id: str,
    project_id: str | None,
    run_id: str | None,
    issue_key: str | None,
    agent_id: str,
    invocation_id: str | None,
    channel: str | None,
    command: str | None,
    working_dir: str | None,
    stage: str,
    attempt: int | None,
    stream: str,
    message: str,
    recorded_at: datetime | None = None,
    max_events_per_run: int = MAX_PERSISTED_LOG_EVENTS_PER_RUN,
) -> None:
    normalized_event = _normalize_run_log_event(
        tenant_id=tenant_id,
        project_id=project_id,
        run_id=run_id,
        issue_key=issue_key,
        agent_id=agent_id,
        invocation_id=invocation_id,
        channel=channel,
        command=command,
        working_dir=working_dir,
        stage=stage,
        attempt=attempt,
        stream=stream,
        message=message,
        recorded_at=recorded_at,
    )
    if normalized_event is None:
        return
    session.add(_build_run_log_model(normalized_event))
    session.flush()
    _prune_invocation_events(
        session=session,
        invocation_id=normalized_event.invocation_id,
        max_events_per_run=max_events_per_run,
    )
    _notify_normalized_run_log_event(session=session, event=normalized_event)


def record_run_log_events_batch(
    *,
    session: Session,
    events: Iterable[dict[str, object]],
    max_events_per_run: int = MAX_PERSISTED_LOG_EVENTS_PER_RUN,
) -> int:
    normalized_events: list[NormalizedRunLogEvent] = []
    for event in events:
        normalized = _normalize_run_log_event(
            tenant_id=str(event.get("tenant_id") or ""),
            project_id=event.get("project_id") if isinstance(event.get("project_id"), str) else None,
            run_id=event.get("run_id") if isinstance(event.get("run_id"), str) else None,
            issue_key=event.get("issue_key") if isinstance(event.get("issue_key"), str) else None,
            agent_id=str(event.get("agent_id") or ""),
            invocation_id=event.get("invocation_id") if isinstance(event.get("invocation_id"), str) else None,
            channel=event.get("channel") if isinstance(event.get("channel"), str) else None,
            command=event.get("command") if isinstance(event.get("command"), str) else None,
            working_dir=event.get("working_dir") if isinstance(event.get("working_dir"), str) else None,
            stage=str(event.get("stage") or ""),
            attempt=event.get("attempt") if isinstance(event.get("attempt"), int) else None,
            stream=str(event.get("stream") or ""),
            message=str(event.get("message") or ""),
            recorded_at=event.get("recorded_at") if isinstance(event.get("recorded_at"), datetime) else None,
        )
        if normalized is not None:
            normalized_events.append(normalized)
    if not normalized_events:
        return 0

    for normalized_event in normalized_events:
        session.add(_build_run_log_model(normalized_event))
    session.flush()

    for invocation_id in {event.invocation_id for event in normalized_events}:
        _prune_invocation_events(
            session=session,
            invocation_id=invocation_id,
            max_events_per_run=max_events_per_run,
        )
    for normalized_event in normalized_events:
        _notify_normalized_run_log_event(session=session, event=normalized_event)
    return len(normalized_events)
