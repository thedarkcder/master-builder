from __future__ import annotations

from datetime import datetime

from fastapi import HTTPException, status
from sqlalchemy import desc, select

from orchestrator.api.admin.live_telemetry_service import list_live_workflow_telemetry_events
from orchestrator.api.admin.schema_mappers import workflow_observability_event_to_schema
from orchestrator.api.admin.workflow_queries import (
    cursor_filtered_events,
    workflow_by_execution_id,
    workflow_operation_attempts,
)
from orchestrator.api.admin.workflow_transcript_service import build_workflow_step_transcript
from orchestrator.api.schemas import (
    WorkflowObservabilityEventRead,
    WorkflowStepAttemptTranscriptRead,
    WorkflowStepTranscriptRead,
)
from orchestrator.core.config import get_settings
from orchestrator.storage.models import AuditEvent, WorkflowOperation, WorkflowOperationAttempt


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

    query = (
        select(AuditEvent)
        .where(AuditEvent.workflow_id == workflow.workflow_id)
        .order_by(desc(AuditEvent.recorded_at), desc(AuditEvent.event_id))
    )
    if operation_id is not None:
        operation = workflow_operation_for_execution(session=session, workflow=workflow, operation_id=operation_id)
        query = query.where(AuditEvent.operation_id == operation.operation_id)
    rows = session.execute(query).scalars().all()
    return cursor_filtered_events(
        events=[workflow_observability_event_to_schema(row) for row in rows],
        before_recorded_at=before_recorded_at,
        before_event_id=before_event_id,
        limit=limit,
    )


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

    latest_attempt_started_at: datetime | None
    operation = None
    if operation_id is not None:
        operation = workflow_operation_for_execution(session=session, workflow=workflow, operation_id=operation_id)
        latest_attempt = session.execute(
            select(WorkflowOperationAttempt)
            .where(WorkflowOperationAttempt.operation_id == operation.operation_id)
            .order_by(desc(WorkflowOperationAttempt.attempt_number))
            .limit(1)
        ).scalar_one_or_none()
        latest_attempt_started_at = (
            latest_attempt.started_at
            if latest_attempt is not None and latest_attempt.started_at is not None
            else operation.updated_at
        )
    else:
        latest_attempt_started_at = workflow.started_at or workflow.created_at
    return cursor_filtered_events(
        events=list_live_workflow_telemetry_events(
            settings=get_settings(),
            tenant_id=workflow.tenant_id,
            workflow_id=workflow.workflow_id,
            operation_id=(operation.operation_id if operation is not None else None),
            limit=limit,
            start_at=latest_attempt_started_at,
        ),
        before_recorded_at=before_recorded_at,
        before_event_id=before_event_id,
        limit=limit,
    )


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
        audit_query = (
            select(AuditEvent)
            .where(AuditEvent.workflow_id == workflow.workflow_id, AuditEvent.operation_id == operation.operation_id)
            .order_by(desc(AuditEvent.recorded_at), desc(AuditEvent.event_id))
            .limit(limit)
        )
        if normalized_attempt_id is not None:
            audit_query = audit_query.where(AuditEvent.attempt_id == normalized_attempt_id)
        audit_rows = list(session.execute(audit_query).scalars().all())
        audit_rows.reverse()
        audit_events = [workflow_observability_event_to_schema(event) for event in audit_rows]
    else:
        started_candidates = [attempt.started_at for attempt in attempts if attempt.started_at is not None]
        telemetry_start_at = min(started_candidates) if started_candidates else operation.updated_at
        telemetry_events = list_live_workflow_telemetry_events(
            settings=get_settings(),
            tenant_id=workflow.tenant_id,
            workflow_id=workflow.workflow_id,
            operation_id=operation.operation_id,
            limit=limit,
            start_at=telemetry_start_at,
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
