from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

from fastapi import HTTPException, status

from orchestrator.core.log_event_bus import (
    build_run_stream_matcher,
    build_run_stream_snapshot_query,
    encode_stream_row,
    get_run_stream_broker,
    stream_from_subscriber,
)


def stream_run_events_ndjson(
    *,
    session,
    run_id: str,
    run_model,
    settings,
    psycopg_module,
) -> Iterator[str]:  # noqa: ANN001
    run = session.get(run_model, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

    initial_event_limit = max(1, min(int(getattr(settings, "run_events_initial_limit", 100)), 500))
    initial_log_limit = max(1, min(int(getattr(settings, "run_logs_initial_limit", 200)), 1000))
    snapshot_rows = build_run_stream_snapshot_query(
        run_id=run_id,
        initial_event_limit=initial_event_limit,
        initial_log_limit=initial_log_limit,
    )
    for row in snapshot_rows:
        payload = encode_stream_row(row)
        if payload is not None:
            yield payload

    if not bool(getattr(settings, "log_bus_enabled", False)):
        return

    subscriber_id = f"run-stream::{run_id}::{uuid4().hex}"
    broker = get_run_stream_broker()
    subscriber = broker.subscribe(
        subscriber_id=subscriber_id,
        buffer_size=max(1, int(getattr(settings, "log_subscriber_buffer_size", 256))),
        match_fn=build_run_stream_matcher(run_id=run_id),
        render_fn=encode_stream_row,
    )
    try:
        yield from stream_from_subscriber(subscriber=subscriber)
    finally:
        broker.unsubscribe(subscriber_id)
