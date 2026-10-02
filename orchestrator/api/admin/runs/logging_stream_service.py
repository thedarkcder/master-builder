from __future__ import annotations

from collections.abc import Iterator

from fastapi import HTTPException, status

from orchestrator.core.observability.stream import stream_product_event_rows
from orchestrator.core.observability.logging_pane import (
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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Run not found"
        )

    initial_event_limit = max(
        1, min(int(getattr(settings, "run_events_initial_limit", 100)), 500)
    )
    initial_log_limit = max(
        1, min(int(getattr(settings, "logging_pane_initial_log_limit", 200)), 1000)
    )
    yield from stream_product_event_rows(
        snapshot_loader=lambda: build_run_logging_stream_snapshot_query(
            session=session,
            run_id=run_id,
            initial_event_limit=initial_event_limit,
            initial_log_limit=initial_log_limit,
        ),
        after_cursor_loader=lambda cursor: list_logging_pane_events_after_sequence(
            filters={"run_id": run_id},
            after_sequence=cursor,
            limit=500,
        ),
        encoder=encode_logging_pane_stream_row,
        batch_limit=500,
    )
