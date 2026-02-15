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
    run_id: str,
    issue_key: str | None,
    agent_id: str,
    stage: str,
    attempt: int | None,
    stream: str,
    message: str,
    recorded_at: datetime | None = None,
    max_events_per_run: int = MAX_PERSISTED_LOG_EVENTS_PER_RUN,
) -> None:
    normalized_message = str(message or "").strip()
    if not normalized_message:
        return
    normalized_message = normalized_message[:MAX_LOG_MESSAGE_CHARS]
    timestamp = recorded_at or datetime.now(timezone.utc)
    normalized_stream = str(stream or "").strip().lower() or "stdout"
    if normalized_stream not in {"stdout", "stderr", "system"}:
        normalized_stream = "stdout"
    normalized_stage = str(stage or "").strip().lower() or "unknown"
    normalized_agent = str(agent_id or "").strip() or "unknown-agent"

    session.add(
        RunLogEvent(
            event_id=uuid4().hex,
            tenant_id=str(tenant_id or "").strip(),
            project_id=str(project_id or "").strip() or None,
            run_id=str(run_id or "").strip(),
            issue_key=str(issue_key or "").strip() or None,
            agent_id=normalized_agent,
            stage=normalized_stage,
            attempt=attempt,
            stream=normalized_stream,
            message=normalized_message,
            recorded_at=timestamp,
        )
    )
    session.flush()
    cutoff_ids = session.execute(
        select(RunLogEvent.event_id)
        .where(RunLogEvent.run_id == run_id)
        .order_by(desc(RunLogEvent.recorded_at), desc(RunLogEvent.event_id))
        .offset(max(0, max_events_per_run))
    ).scalars().all()
    if cutoff_ids:
        session.execute(delete(RunLogEvent).where(RunLogEvent.event_id.in_(cutoff_ids)))

    notify_run_log_event(
        session,
        tenant_id=tenant_id,
        run_id=run_id,
        issue_key=issue_key,
        project_id=project_id,
        agent_id=normalized_agent,
        stage=normalized_stage,
        attempt=attempt,
        stream=normalized_stream,
        message=normalized_message,
        recorded_at=timestamp.isoformat(),
    )
