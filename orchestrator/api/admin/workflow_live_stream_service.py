from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

from fastapi import HTTPException, status

from orchestrator.api.schemas import WorkflowObservabilityEventRead
from orchestrator.core.observability_stream import (
    build_observability_stream_matcher,
    build_observability_snapshot_query,
    encode_stream_row,
    get_observability_stream_broker,
    list_operation_observability_events,
    observability_stream_event_to_payload,
    stream_from_subscriber,
)


def list_workflow_operation_live_events(
    *,
    session,
    operation,
    attempt_id: str | None = None,
    limit: int = 500,
):  # noqa: ANN001
    if operation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow operation not found")
    rows = list_operation_observability_events(
        session=session,
        operation_id=operation.operation_id,
        attempt_id=attempt_id,
        limit=limit,
    )
    return [WorkflowObservabilityEventRead(**observability_stream_event_to_payload(row)) for row in rows]


def stream_workflow_operation_live_events_ndjson(
    *,
    operation,
    settings,
    attempt_id: str | None = None,
) -> Iterator[str]:  # noqa: ANN001
    if operation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow operation not found")

    snapshot_rows = build_observability_snapshot_query(
        operation_id=operation.operation_id,
        attempt_id=attempt_id,
        limit=max(1, min(int(getattr(settings, "run_logs_initial_limit", 200)), 1000)),
    )
    snapshot_max_offset = max((int(row.stream_offset) for row in snapshot_rows), default=0)
    for row in snapshot_rows:
        yield encode_stream_row(row)

    if not bool(getattr(settings, "log_bus_enabled", False)):
        return

    subscriber_id = f"workflow-live::{operation.operation_id}::{attempt_id or 'all'}::{uuid4().hex}"
    broker = get_observability_stream_broker()
    subscriber = broker.subscribe(
        subscriber_id=subscriber_id,
        buffer_size=max(1, int(getattr(settings, "log_subscriber_buffer_size", 256))),
        match_fn=build_observability_stream_matcher(
            operation_id=operation.operation_id,
            attempt_id=attempt_id,
        ),
        render_fn=encode_stream_row,
        min_stream_offset_exclusive=snapshot_max_offset,
    )
    try:
        yield from stream_from_subscriber(subscriber=subscriber)
    finally:
        broker.unsubscribe(subscriber_id)
