from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from orchestrator.core.observability_stream import record_observability_stream_event
from orchestrator.core.workflow_attempt_ref import WorkflowAttemptRef
from orchestrator.storage.models import WorkflowExecution, WorkflowOperation

_OPERATION_LOGGER = logging.getLogger("orchestrator.workflow_operation")


def emit_workflow_operation_log(
    session: Session,
    *,
    operation: WorkflowOperation,
    event_type: str,
    message: str,
    level: int = logging.INFO,
    attempt_ref: WorkflowAttemptRef,
    metadata: dict[str, Any] | None = None,
) -> None:
    workflow = session.get(WorkflowExecution, operation.workflow_id)
    if workflow is None:
        raise ValueError(f"Workflow {operation.workflow_id} is missing for operation telemetry event.")
    attempt_ref.assert_matches_operation(operation.operation_id)
    attempt_id = attempt_ref.require_attempt_id()

    event_metadata: dict[str, Any] = {
        "workflow_id": workflow.workflow_id,
        "execution_id": workflow.execution_id,
        "operation_id": operation.operation_id,
        "operation_type": operation.operation_type,
        "source_system": workflow.source_system,
        "source_ref": workflow.source_ref,
        "run_id": operation.run_id,
        "attempt_id": attempt_id,
        "attempt_number": attempt_ref.number,
    }
    if isinstance(metadata, dict):
        event_metadata.update(metadata)

    _OPERATION_LOGGER.log(
        level,
        str(message or "").strip(),
        extra={
            "event_type": str(event_type or "").strip() or "workflow_operation",
            "tenant_id": workflow.tenant_id,
            "project_id": workflow.project_id,
            "metadata": event_metadata,
        },
    )
    record_observability_stream_event(
        session,
        tenant_id=workflow.tenant_id,
        project_id=workflow.project_id,
        workflow_id=workflow.workflow_id,
        run_id=operation.run_id,
        operation_id=operation.operation_id,
        attempt_id=attempt_id,
        issue_key=workflow.source_ref,
        event_kind=str(event_type or "").strip() or "workflow_operation",
        level=logging.getLevelName(level).lower(),
        source_component="workflow_operation",
        message=str(message or "").strip(),
        payload=event_metadata,
    )
