from __future__ import annotations

from datetime import datetime
import json

from sqlalchemy.orm import Session

from orchestrator.core.observability.events import (
    EventCursor,
    ProductEvent,
    initialize_product_event_store,
    list_product_events,
    record_product_event,
)


def record_observability_stream_event(
    session: Session,
    *,
    tenant_id: str,
    project_id: str | None,
    workflow_id: str | None,
    run_id: str | None,
    operation_id: str | None,
    attempt_id: str | None,
    issue_key: str | None,
    event_kind: str,
    level: str,
    source_component: str | None,
    message: str,
    payload: dict | None = None,
    recorded_at: datetime | None = None,
) -> ProductEvent:
    return record_product_event(
        session,
        event_class="execution_log",
        tenant_id=tenant_id,
        project_id=project_id,
        workflow_id=workflow_id,
        run_id=run_id,
        operation_id=operation_id,
        attempt_id=attempt_id,
        issue_key=issue_key,
        event_kind=event_kind,
        level=level,
        source_component=source_component,
        message=message,
        payload=payload,
        recorded_at=recorded_at,
    )


def observability_stream_event_to_payload(row: ProductEvent) -> dict[str, object]:
    payload = dict(row.payload_json or {})
    attempt_number: int | None = None
    raw_attempt = payload.get("attempt")
    if isinstance(raw_attempt, int):
        attempt_number = raw_attempt
    elif isinstance(raw_attempt, str):
        try:
            attempt_number = int(raw_attempt)
        except ValueError:
            attempt_number = None
    return {
        "event_id": f"telemetry:{row.event_sequence}",
        "event_sequence": row.event_sequence,
        "source": "telemetry",
        "level": row.level,
        "event_kind": row.event_kind,
        "message": row.message,
        "source_component": row.source_component,
        "run_id": row.run_id,
        "operation_id": row.operation_id,
        "attempt_id": row.attempt_id,
        "agent_id": str(payload.get("agent_id") or "").strip() or None,
        "invocation_id": str(payload.get("invocation_id") or "").strip() or None,
        "stage": str(payload.get("stage") or "").strip() or None,
        "attempt": attempt_number,
        "stream": str(payload.get("stream") or "").strip() or None,
        "payload": payload,
        "recorded_at": row.recorded_at.isoformat(),
    }


def build_observability_snapshot_query(
    *,
    operation_id: str,
    attempt_id: str | None = None,
    limit: int = 500,
) -> list[ProductEvent]:
    filters = {"operation_id": str(operation_id or "").strip() or None}
    if attempt_id:
        filters["attempt_id"] = str(attempt_id or "").strip() or None
    rows = list_product_events(
        event_class="execution_log",
        filters=filters,
        limit=limit,
        newest_first=True,
    )
    return list(reversed(rows))


def list_operation_observability_events(
    *,
    session: Session,
    operation_id: str,
    attempt_id: str | None = None,
    limit: int = 500,
) -> list[ProductEvent]:
    del session
    filters = {"operation_id": str(operation_id or "").strip() or None}
    if attempt_id:
        filters["attempt_id"] = str(attempt_id or "").strip() or None
    rows = list_product_events(
        event_class="execution_log",
        filters=filters,
        limit=limit,
        newest_first=True,
    )
    return rows


def list_workflow_observability_events(
    *,
    workflow_id: str,
    operation_id: str | None = None,
    limit: int = 200,
    before_recorded_at: datetime | None = None,
    before_event_id: str | None = None,
) -> list[ProductEvent]:
    before_sequence = _event_sequence_from_id(before_event_id)
    filters = {"workflow_id": str(workflow_id or "").strip() or None}
    if operation_id:
        filters["operation_id"] = str(operation_id or "").strip() or None
    return list_product_events(
        event_class="execution_log",
        filters=filters,
        limit=limit,
        before=EventCursor(
            recorded_at=before_recorded_at, event_sequence=before_sequence
        ),
        newest_first=True,
    )


def _event_sequence_from_id(event_id: str | None) -> int | None:
    normalized = str(event_id or "").strip()
    if normalized.startswith("telemetry:"):
        normalized = normalized.removeprefix("telemetry:")
    if not normalized:
        return None
    try:
        return int(normalized)
    except ValueError:
        return None


def encode_stream_row(row: ProductEvent) -> str:
    return (
        json.dumps(observability_stream_event_to_payload(row), separators=(",", ":"))
        + "\n"
    )


def initialize_observability_streaming() -> None:
    initialize_product_event_store()


def shutdown_observability_streaming() -> None:
    return None
