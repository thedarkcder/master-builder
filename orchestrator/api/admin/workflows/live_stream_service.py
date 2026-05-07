from __future__ import annotations

from collections.abc import Iterator
from fastapi import HTTPException, status

from orchestrator.api.schemas import WorkflowObservabilityEventRead
from orchestrator.core.observability.observability_stream import (
    build_observability_snapshot_query,
    encode_stream_row,
    list_operation_observability_events,
    observability_stream_event_to_payload,
)
from orchestrator.core.observability.stream import stream_product_event_rows
from orchestrator.core.observability.events import list_product_events_after_sequence
from orchestrator.storage.models import WorkflowOperationAttempt


def list_workflow_operation_live_events(
    *,
    session,
    operation,
    attempt_id: str | None = None,
    limit: int = 500,
):  # noqa: ANN001
    if operation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow operation not found")
    normalized_attempt_id = str(attempt_id or "").strip() or None
    if normalized_attempt_id is not None:
        attempt = session.get(WorkflowOperationAttempt, normalized_attempt_id)
        if attempt is None or attempt.operation_id != operation.operation_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow operation attempt not found")
    rows = list_operation_observability_events(
        session=session,
        operation_id=operation.operation_id,
        attempt_id=normalized_attempt_id,
        limit=limit,
    )
    return [WorkflowObservabilityEventRead(**observability_stream_event_to_payload(row)) for row in rows]


def stream_workflow_operation_live_events_ndjson(
    *,
    operation_id: str,
    settings,
    attempt_id: str | None = None,
    after_event_sequence: int | None = None,
) -> Iterator[str]:  # noqa: ANN001
    normalized_operation_id = str(operation_id or "").strip()
    if not normalized_operation_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow operation not found")

    filters = {"operation_id": normalized_operation_id}
    if attempt_id:
        filters["attempt_id"] = attempt_id

    yield from stream_product_event_rows(
        snapshot_loader=lambda: build_observability_snapshot_query(
            operation_id=normalized_operation_id,
            attempt_id=attempt_id,
            limit=max(1, min(int(getattr(settings, "logging_pane_initial_log_limit", 200)), 1000)),
        ),
        after_cursor_loader=lambda cursor: list_product_events_after_sequence(
            event_class="execution_log",
            filters=filters,
            after_sequence=cursor,
            limit=500,
        ),
        encoder=encode_stream_row,
        after_event_sequence=after_event_sequence,
        batch_limit=500,
    )
