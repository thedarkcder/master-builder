from __future__ import annotations

from datetime import datetime

from fastapi import HTTPException, status

from orchestrator.api.admin.schema_mappers import workflow_observability_event_to_schema
from orchestrator.api.admin.workflow_live_stream_service import list_workflow_operation_live_events
from orchestrator.api.admin.workflow_queries import (
    workflow_by_execution_id,
    workflow_operation_attempts,
)
from orchestrator.api.admin.workflow_transcript_service import build_workflow_step_transcript
from orchestrator.api.schemas import (
    WorkflowObservabilityEventRead,
    WorkflowStepAttemptTranscriptRead,
    WorkflowStepTranscriptRead,
)
from orchestrator.core.observability_stream import (
    list_workflow_observability_events,
    observability_stream_event_to_payload,
)
from orchestrator.core.product_events import EventCursor, list_product_events
from orchestrator.storage.models import WorkflowOperation


def workflow_operation_for_execution(
    *,
    session,
    workflow,
    operation_id: str,
) -> WorkflowOperation:  # noqa: ANN001
    operation = session.get(WorkflowOperation, str(operation_id or "").strip())
    if operation is None or operation.workflow_id != workflow.workflow_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow operation not found")
    return operation


def list_workflow_audit_events(
    *,
    session,
    execution_id: str,
    operation_id: str | None = None,
    limit: int = 200,
    before_recorded_at: datetime | None = None,
    before_event_id: str | None = None,
) -> list[WorkflowObservabilityEventRead]:
    workflow = workflow_by_execution_id(session=session, execution_id=execution_id)
    if workflow is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found")

    filters = {"workflow_id": workflow.workflow_id}
    if operation_id is not None:
        operation = workflow_operation_for_execution(session=session, workflow=workflow, operation_id=operation_id)
        filters["operation_id"] = operation.operation_id
    rows = list_product_events(
        event_class="audit_evidence",
        filters=filters,
        before=EventCursor(
            recorded_at=before_recorded_at,
            event_sequence=_event_sequence_from_id(before_event_id, prefix="audit:"),
        ),
        limit=limit,
        newest_first=True,
    )
    return [workflow_observability_event_to_schema(row) for row in rows]


def list_workflow_telemetry_events(
    *,
    session,
    execution_id: str,
    operation_id: str | None = None,
    limit: int = 200,
    before_recorded_at: datetime | None = None,
    before_event_id: str | None = None,
) -> list[WorkflowObservabilityEventRead]:
    workflow = workflow_by_execution_id(session=session, execution_id=execution_id)
    if workflow is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found")

    operation = None
    if operation_id is not None:
        operation = workflow_operation_for_execution(session=session, workflow=workflow, operation_id=operation_id)
    rows = list_workflow_observability_events(
        workflow_id=workflow.workflow_id,
        operation_id=operation.operation_id if operation is not None else None,
        limit=limit,
        before_recorded_at=before_recorded_at,
        before_event_id=before_event_id,
    )
    return [WorkflowObservabilityEventRead(**observability_stream_event_to_payload(row)) for row in rows]


def get_workflow_step_transcript(
    *,
    session,
    execution_id: str,
    operation_id: str,
    source: str,
    attempt_id: str | None = None,
    limit: int = 500,
) -> WorkflowStepTranscriptRead:
    workflow = workflow_by_execution_id(session=session, execution_id=execution_id)
    if workflow is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found")
    operation = workflow_operation_for_execution(session=session, workflow=workflow, operation_id=operation_id)
    attempts = workflow_operation_attempts(session=session, operation_id=operation.operation_id)
    normalized_source = str(source or "").strip().lower()
    if normalized_source not in {"audit", "telemetry"}:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unsupported transcript source")
    normalized_attempt_id = str(attempt_id or "").strip() or None
    if normalized_attempt_id is not None:
        attempts = [attempt for attempt in attempts if attempt.attempt_id == normalized_attempt_id]
        if not attempts:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow operation attempt not found")

    audit_events: list[WorkflowObservabilityEventRead] = []
    telemetry_events: list[WorkflowObservabilityEventRead] = []
    if normalized_source == "audit":
        filters = {"workflow_id": workflow.workflow_id, "operation_id": operation.operation_id}
        if normalized_attempt_id is not None:
            filters["attempt_id"] = normalized_attempt_id
        audit_rows = list_product_events(
            event_class="audit_evidence",
            filters=filters,
            limit=limit,
            newest_first=False,
        )
        audit_events = [workflow_observability_event_to_schema(event) for event in audit_rows]
    else:
        telemetry_events = list_workflow_operation_live_events(
            session=session,
            operation=operation,
            attempt_id=normalized_attempt_id,
            limit=limit,
        )
    return build_workflow_step_transcript(
        session=session,
        workflow=workflow,
        operation=operation,
        attempts=attempts,
        telemetry_events=telemetry_events,
        audit_events=audit_events,
        source="telemetry" if normalized_source == "telemetry" else "audit",
    )


def get_workflow_step_audit_attempt(
    *,
    session,
    execution_id: str,
    operation_id: str,
    attempt_id: str,
    limit: int = 500,
) -> WorkflowStepAttemptTranscriptRead:
    transcript = get_workflow_step_transcript(
        session=session,
        execution_id=execution_id,
        operation_id=operation_id,
        source="audit",
        attempt_id=attempt_id,
        limit=limit,
    )
    if not transcript.attempts:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow operation attempt not found")
    return transcript.attempts[0]


def _event_sequence_from_id(event_id: str | None, *, prefix: str) -> int | None:
    normalized = str(event_id or "").strip()
    if normalized.startswith(prefix):
        normalized = normalized.removeprefix(prefix)
    if not normalized:
        return None
    try:
        return int(normalized)
    except ValueError:
        return None
