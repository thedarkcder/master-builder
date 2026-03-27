from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

from sqlalchemy import desc, select

from orchestrator.core.log_event_bus import (
    build_codex_stream_matcher,
    build_codex_stream_snapshot_query,
    encode_stream_row,
    get_run_stream_broker,
    stream_from_subscriber,
)
from orchestrator.storage.models import RunLogEvent


def list_codex_log_events(
    *,
    session,
    run_log_schema_cls,
    tenant_id: str | None = None,
    project_id: str | None = None,
    run_id: str | None = None,
    channel: str | None = None,
    command: str | None = None,
    limit: int = 500,
):  # noqa: ANN001
    query = select(RunLogEvent).order_by(desc(RunLogEvent.recorded_at), desc(RunLogEvent.event_id))
    if tenant_id:
        query = query.where(RunLogEvent.tenant_id == tenant_id)
    if project_id:
        query = query.where(RunLogEvent.project_id == project_id)
    if run_id:
        query = query.where(RunLogEvent.run_id == run_id)
    if channel:
        query = query.where(RunLogEvent.channel == channel)
    if command:
        query = query.where(RunLogEvent.command == command)
    rows = session.execute(query.limit(max(1, min(limit, 2000)))).scalars().all()
    return [
        run_log_schema_cls(
            run_id=row.run_id,
            issue_key=row.issue_key,
            project_id=row.project_id,
            agent_id=row.agent_id,
            invocation_id=row.invocation_id,
            channel=row.channel,
            command=row.command,
            working_dir=row.working_dir,
            stage=row.stage,
            attempt=row.attempt,
            stream=row.stream,
            message=row.message,
            recorded_at=row.recorded_at,
        )
        for row in rows
    ]


def stream_codex_events_ndjson(
    *,
    session,
    settings,
    psycopg_module,
    tenant_id: str | None = None,
    project_id: str | None = None,
    run_id: str | None = None,
    channel: str | None = None,
    command: str | None = None,
) -> Iterator[str]:  # noqa: ANN001
    rows = build_codex_stream_snapshot_query(
        tenant_id=tenant_id,
        project_id=project_id,
        run_id=run_id,
        channel=channel,
        command=command,
        limit=500,
    )
    for row in rows:
        payload = encode_stream_row(row)
        if payload is not None:
            yield payload

    if not bool(getattr(settings, "log_bus_enabled", False)):
        return

    subscriber_id = f"codex-stream::{uuid4().hex}"
    broker = get_run_stream_broker()
    subscriber = broker.subscribe(
        subscriber_id=subscriber_id,
        buffer_size=max(1, int(getattr(settings, "log_subscriber_buffer_size", 256))),
        match_fn=build_codex_stream_matcher(
            tenant_id=tenant_id,
            project_id=project_id,
            run_id=run_id,
            channel=channel,
            command=command,
        ),
        render_fn=encode_stream_row,
    )
    try:
        yield from stream_from_subscriber(subscriber=subscriber)
    finally:
        broker.unsubscribe(subscriber_id)
