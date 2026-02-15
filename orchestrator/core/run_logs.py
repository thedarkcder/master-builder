from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import delete, desc, select
from sqlalchemy.orm import Session

from orchestrator.storage.models import RunLogEvent
from orchestrator.storage.run_event_stream import notify_run_log_event

MAX_PERSISTED_LOG_EVENTS_PER_RUN = 5000
MAX_LOG_MESSAGE_CHARS = 2000


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
    normalized_tenant = str(tenant_id or "").strip()
    if not normalized_tenant:
        raise ValueError("Codex log event requires tenant_id")
    normalized_message = str(message or "").strip()
    if not normalized_message:
        return
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

    session.add(
        RunLogEvent(
            event_id=uuid4().hex,
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
    )
    session.flush()
    cutoff_query = (
        select(RunLogEvent.event_id)
        .where(RunLogEvent.invocation_id == normalized_invocation_id)
        .order_by(desc(RunLogEvent.recorded_at), desc(RunLogEvent.event_id))
        .offset(max(0, max_events_per_run))
    )
    cutoff_ids = session.execute(cutoff_query).scalars().all()
    if cutoff_ids:
        session.execute(delete(RunLogEvent).where(RunLogEvent.event_id.in_(cutoff_ids)))

    notify_run_log_event(
        session,
        tenant_id=normalized_tenant,
        run_id=normalized_run_id,
        issue_key=issue_key,
        project_id=project_id,
        agent_id=normalized_agent,
        invocation_id=normalized_invocation_id,
        channel=normalized_channel,
        command=normalized_command,
        working_dir=normalized_working_dir,
        stage=normalized_stage,
        attempt=attempt,
        stream=normalized_stream,
        message=normalized_message,
        recorded_at=timestamp.isoformat(),
    )
