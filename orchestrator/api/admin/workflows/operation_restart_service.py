from __future__ import annotations

from fastapi import HTTPException, status

from orchestrator.api.admin.schema_mappers import workflow_operation_attempt_to_schema
from orchestrator.api.admin.workflows.execution_read_service import workflow_schema
from orchestrator.api.admin.workflows.operation_retry_service import build_registered_operation_retry_runtime
from orchestrator.api.admin.workflows.queries import (
    workflow_by_execution_id,
    workflow_operation_attempts,
    workflow_operation_attempts_by_operation,
)
from orchestrator.api.schemas import WorkflowOperationRetryRead
from orchestrator.core.workflow.advance import (
    InvalidWorkflowOperationRetryError,
    UnsupportedWorkflowOperationRetryError,
)
from orchestrator.core.workflow.handler_composition import installed_operation_retry_capabilities
from orchestrator.core.workflow.operation_service import (
    OPERATION_STATUS_RUNNING,
    OPERATION_STATUS_WAITING_FOR_INPUT,
    WorkflowOperationAttemptAlreadyRunningError,
    fail_workflow_operation,
)
from orchestrator.core.workflow.type_catalog import get_workflow_type
from orchestrator.storage.models import WorkflowOperation


def _assert_restart_supported(*, session, workflow, operation: WorkflowOperation) -> None:  # noqa: ANN001
    workflow_type = get_workflow_type(session, workflow_type_key=workflow.workflow_type_key)
    definitions_by_key = {definition.key: definition for definition in workflow_type.steps}
    definition = definitions_by_key.get(str(operation.operation_type or "").strip())
    if definition is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Workflow operation is not defined by registered code")
    if not definition.retryable:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Workflow operation restart is disabled by policy")
    executable_retry_types = {
        capability.operation_type
        for capability in installed_operation_retry_capabilities(workflow_type=workflow_type)
    }
    if definition.key not in executable_retry_types:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No executable restart handler is registered for this operation",
        )


def restart_workflow_operation(
    *,
    session,
    execution_id: str,
    operation_id: str,
    actor: str,
    restart_reason: str,
    workflow_to_schema_fn,
    run_to_schema_fn,
    integration_router,
    build_runtime_for_selector_fn,
    seed_issues_with_runtime_fn,
):  # noqa: ANN001
    workflow = workflow_by_execution_id(session=session, execution_id=execution_id)
    if workflow is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found")

    operation = session.get(WorkflowOperation, operation_id)
    if operation is None or operation.workflow_id != workflow.workflow_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow operation not found")

    _assert_restart_supported(session=session, workflow=workflow, operation=operation)

    attempts_by_operation = workflow_operation_attempts_by_operation(session=session, workflow_id=workflow.workflow_id)
    latest_attempt = (attempts_by_operation.get(operation.operation_id) or [None])[-1]
    if latest_attempt is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Workflow operation has no active attempt to restart")
    latest_status = str(latest_attempt.status or "").strip().lower()
    if latest_status == OPERATION_STATUS_WAITING_FOR_INPUT:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Workflow operation is waiting for input and cannot be restarted",
        )
    if latest_status != OPERATION_STATUS_RUNNING:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Workflow operation has no running attempt to restart")
    previous_latest_attempt_id = str(latest_attempt.attempt_id or "").strip()

    reason = str(restart_reason or "").strip() or "Restarted stale running workflow operation attempt."
    fail_workflow_operation(
        session,
        operation=operation,
        attempt=latest_attempt,
        category="interrupted",
        message=f"{reason} Actor: {actor}.",
    )
    session.commit()
    session.refresh(workflow)
    session.refresh(operation)
    runtime = build_registered_operation_retry_runtime(
        session=session,
        integration_router=integration_router,
        build_runtime_for_selector_fn=build_runtime_for_selector_fn,
        seed_issues_with_runtime_fn=seed_issues_with_runtime_fn,
    )
    try:
        runtime.retry_operation(workflow=workflow, operation=operation)
    except (
        InvalidWorkflowOperationRetryError,
        UnsupportedWorkflowOperationRetryError,
        WorkflowOperationAttemptAlreadyRunningError,
    ) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    session.commit()
    refreshed_workflow = workflow_schema(
        session=session,
        workflow=workflow,
        workflow_to_schema_fn=workflow_to_schema_fn,
        run_to_schema_fn=run_to_schema_fn,
    )
    refreshed_attempts = workflow_operation_attempts(session=session, operation_id=operation.operation_id)
    latest_attempt = refreshed_attempts[0] if refreshed_attempts else None
    started_attempt = (
        latest_attempt
        if latest_attempt is not None and str(latest_attempt.attempt_id or "").strip() != previous_latest_attempt_id
        else None
    )
    if started_attempt is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Workflow operation restart did not create a new persisted attempt",
        )
    return WorkflowOperationRetryRead(
        workflow=refreshed_workflow,
        started_attempt=workflow_operation_attempt_to_schema(started_attempt),
    )
