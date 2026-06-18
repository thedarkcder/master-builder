from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.core.workflow.operation_service import OPERATION_STATUS_COMPLETED, OPERATION_STATUS_WAITING_FOR_INPUT
from orchestrator.core.workflow.type_catalog import get_workflow_type
from orchestrator.storage.models import WorkflowExecution, WorkflowOperation


def _now() -> datetime:
    return datetime.now(timezone.utc)


def mark_workflow_running(*, workflow: WorkflowExecution, now: datetime | None = None) -> WorkflowExecution:
    timestamp = now or _now()
    workflow.status = "running"
    workflow.last_error = None
    workflow.started_at = workflow.started_at or timestamp
    workflow.finished_at = None
    workflow.updated_at = timestamp
    return workflow


def mark_workflow_waiting_for_input(
    *,
    workflow: WorkflowExecution,
    now: datetime | None = None,
) -> WorkflowExecution:
    timestamp = now or _now()
    workflow.status = "waiting_for_input"
    workflow.last_error = None
    workflow.finished_at = None
    workflow.updated_at = timestamp
    return workflow


def mark_workflow_failed(
    *,
    workflow: WorkflowExecution,
    message: str,
    now: datetime | None = None,
) -> WorkflowExecution:
    timestamp = now or _now()
    workflow.status = "failed"
    workflow.last_error = message
    workflow.finished_at = timestamp
    workflow.updated_at = timestamp
    return workflow


def mark_workflow_completed(
    *,
    workflow: WorkflowExecution,
    now: datetime | None = None,
) -> WorkflowExecution:
    timestamp = now or _now()
    workflow.status = "completed"
    workflow.last_error = None
    workflow.finished_at = timestamp
    workflow.updated_at = timestamp
    return workflow


def recompute_workflow_status(
    *,
    session: Session,
    workflow: WorkflowExecution,
    now: datetime | None = None,
) -> WorkflowExecution:
    timestamp = now or _now()
    definitions = get_workflow_type(session, workflow_type_key=workflow.workflow_type_key).steps
    operations = session.execute(
        select(WorkflowOperation).where(WorkflowOperation.workflow_id == workflow.workflow_id)
    ).scalars().all()
    status_by_type = {
        str(operation.operation_type or "").strip(): str(operation.status or "").strip().lower()
        for operation in operations
        if str(operation.operation_type or "").strip()
    }
    summaries_by_type = {
        str(operation.operation_type or "").strip(): str(operation.summary or "").strip()
        for operation in operations
        if str(operation.operation_type or "").strip()
    }
    required_definitions = [definition for definition in definitions if bool(definition.required)]

    for definition in required_definitions:
        normalized_status = status_by_type.get(definition.key, "pending")
        if normalized_status == "failed":
            return mark_workflow_failed(
                workflow=workflow,
                message=summaries_by_type.get(definition.key) or workflow.last_error or "",
                now=timestamp,
            )
        if normalized_status == OPERATION_STATUS_WAITING_FOR_INPUT:
            return mark_workflow_waiting_for_input(workflow=workflow, now=timestamp)

    if required_definitions and all(
        status_by_type.get(definition.key) == OPERATION_STATUS_COMPLETED
        for definition in required_definitions
    ):
        return mark_workflow_completed(workflow=workflow, now=timestamp)

    return mark_workflow_running(workflow=workflow, now=timestamp)
