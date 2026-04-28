from __future__ import annotations

from fastapi import HTTPException, status

from orchestrator.api.admin.schema_mappers import workflow_operation_attempt_to_schema
from orchestrator.api.admin.workflow_execution_read_service import workflow_schema
from orchestrator.api.admin.workflow_queries import (
    workflow_by_execution_id,
    workflow_operation_attempts,
    workflow_operation_attempts_by_operation,
)
from orchestrator.api.schemas import WorkflowOperationRetryRead
from orchestrator.api.webhooks.contracts import create_jira_comment, post_jira_comment
from orchestrator.core.config import get_settings
from orchestrator.core.workflow_advance import (
    InvalidWorkflowOperationRetryError,
    UnsupportedWorkflowOperationRetryError,
)
from orchestrator.core.workflow_handler_composition import build_installed_workflow_handler_registry
from orchestrator.core.workflow_runtime import build_workflow_runtime
from orchestrator.core.worker.execution_service import build_run_process_kwargs
from orchestrator.core.worker.process_service import process_claimed_run
from orchestrator.core.worker.runtime_factory import build_workflow_runner_for_session
from orchestrator.storage.models import WorkflowOperation


def retry_workflow_operation(
    *,
    session,
    execution_id: str,
    operation_id: str,
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

    attempts_by_operation = workflow_operation_attempts_by_operation(session=session, workflow_id=workflow.workflow_id)
    latest_attempt = (attempts_by_operation.get(operation.operation_id) or [None])[-1]
    if latest_attempt is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Workflow operation has no attempt history to retry")
    if str(latest_attempt.status or "").strip().lower() not in {"failed", "retrying"}:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Workflow operation is not in a failed state")

    handler_registry = build_installed_workflow_handler_registry(
        integration_router=integration_router,
        extract_changed_fields_fn=lambda *_args, **_kwargs: [],
        extract_status_transition_fn=lambda *_args, **_kwargs: (None, None),
        build_runtime_for_selector_fn=build_runtime_for_selector_fn,
        seed_issues_with_runtime_fn=seed_issues_with_runtime_fn,
        post_jira_comment_fn=post_jira_comment,
        create_jira_comment_fn=create_jira_comment,
    )
    runtime = build_workflow_runtime(
        session=session,
        settings=get_settings(),
        process_claimed_run_fn=process_claimed_run,
        build_runner_fn=build_workflow_runner_for_session,
        runtime_kwargs_fn=build_run_process_kwargs,
        resolve_advance_handler_fn=handler_registry.resolve_advance_handler,
        workflow_handler_registry=handler_registry,
    )
    try:
        runtime.retry_operation(workflow=workflow, operation=operation)
    except (InvalidWorkflowOperationRetryError, UnsupportedWorkflowOperationRetryError) as exc:
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
    return WorkflowOperationRetryRead(
        workflow=refreshed_workflow,
        started_attempt=(workflow_operation_attempt_to_schema(latest_attempt) if latest_attempt is not None else None),
    )
