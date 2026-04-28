from __future__ import annotations

from collections.abc import Iterator
from threading import Event

from fastapi import HTTPException, status

from orchestrator.core.logging_pane_events import (
    build_run_logging_stream_snapshot_query,
    encode_logging_pane_stream_row,
    list_logging_pane_events_after_sequence,
)


def stream_run_events_ndjson(
    *,
    session,
    run_id: str,
    run_model,
    settings,
) -> Iterator[str]:  # noqa: ANN001
    run = session.get(run_model, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

    initial_event_limit = max(1, min(int(getattr(settings, "run_events_initial_limit", 100)), 500))
    initial_log_limit = max(1, min(int(getattr(settings, "logging_pane_initial_log_limit", 200)), 1000))
    snapshot_rows = build_run_logging_stream_snapshot_query(
        session=session,
        run_id=run_id,
        initial_event_limit=initial_event_limit,
        initial_log_limit=initial_log_limit,
    )
    cursor = max((int(row.event_sequence) for row in snapshot_rows), default=0)
    for row in snapshot_rows:
        payload = encode_logging_pane_stream_row(row)
        if payload is not None:
            yield payload

    poll_seconds = max(0.1, int(getattr(settings, "event_stream_poll_ms", 500)) / 1000.0)
    stop = Event()
    while not stop.wait(timeout=poll_seconds):
        rows = list_logging_pane_events_after_sequence(
            filters={"run_id": run_id},
            after_sequence=cursor,
            limit=500,
        )
        for row in rows:
            payload = encode_logging_pane_stream_row(row)
            if payload is not None:
                yield payload
        if rows:
            cursor = max(int(row.event_sequence) for row in rows)
