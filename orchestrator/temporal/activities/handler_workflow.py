from __future__ import annotations

from temporalio import activity
from temporalio.exceptions import ApplicationError

from orchestrator.core.config import get_settings
from orchestrator.core.specialist_planning import RetryableSpecialistPlanningContractError
from orchestrator.core.workflow_advance import (
    InvalidWorkflowOperationRetryError,
    UnsupportedWorkflowOperationRetryError,
    WorkflowAdvanceRequest,
    WorkflowTrigger,
    execute_workflow_advance,
)
from orchestrator.core.workflow_execution_projection import WorkflowExecutionReference, WorkflowSourceReference
from orchestrator.core.workflow_operation_retry_use_case import retry_workflow_operation_with_registered_handler
from orchestrator.core.workflow_operation_service import WorkflowOperationAttemptAlreadyRunningError
from orchestrator.core.workflow_type_catalog import get_workflow_type_by_handler_key
from orchestrator.runtime.installed_workflow_handlers import build_runtime_workflow_handler_registry
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import Tenant, WorkflowExecution, WorkflowOperation
from orchestrator.temporal.payloads import (
    HandlerWorkflowAdvanceResult,
    HandlerWorkflowAdvanceInput,
    WorkflowOperationRetryInput,
    WorkflowOperationRetryResult,
)


def _workflow_status_payload(*, workflow: WorkflowExecution) -> dict[str, str | None]:
    return {
        "status": str(workflow.status or "").strip(),
        "active_run_id": str(workflow.active_run_id or "").strip() or None,
        "last_error": str(workflow.last_error or "").strip() or None,
    }


@activity.defn(name="process_handler_workflow_advance_activity")
def process_handler_workflow_advance_activity(
    activity_input: HandlerWorkflowAdvanceInput,
) -> HandlerWorkflowAdvanceResult:
    workflow_id = str(activity_input.workflow_id or "").strip()
    payload = activity_input
    settings = get_settings()
    session_factory = create_session_factory()
    with session_factory() as session:
        tenant = session.get(Tenant, str(payload.tenant_id or "").strip())
        if tenant is None:
            raise RuntimeError(f"Workflow advance is missing tenant {payload.tenant_id}")
        workflow_type = get_workflow_type_by_handler_key(
            session,
            handler_key=str(payload.workflow_handler_key or "").strip(),
        )
        request = WorkflowAdvanceRequest(
            workflow_handler_key=payload.workflow_handler_key,
            tenant_id=payload.tenant_id,
            tenant=tenant,
            project_id=payload.project_id,
            execution=WorkflowExecutionReference(
                key=payload.execution_key,
                source=WorkflowSourceReference(
                    source_system=payload.source_system,
                    source_ref=payload.source_ref,
                    display_name=payload.source_display_name,
                    description=payload.source_description,
                    attributes=dict(payload.source_attributes or {}),
                ),
            ),
            payload=dict(payload.payload or {}),
            trigger=WorkflowTrigger(
                event=payload.trigger_event,
                command=payload.trigger_command,
                argument=payload.trigger_argument,
            ),
        )
        handler_registry = build_runtime_workflow_handler_registry()
        try:
            result = execute_workflow_advance(
                session=session,
                settings=settings,
                workflow_type=workflow_type,
                request=request,
                resolve_advance_handler_fn=handler_registry.resolve_advance_handler,
            )
        except RetryableSpecialistPlanningContractError as exc:
            session.commit()
            raise ApplicationError(
                str(exc),
                type="retryable_invalid_model_output",
            ) from exc
        except Exception as exc:
            raise ApplicationError(
                str(exc),
                type="terminal_workflow_advance_error",
                non_retryable=True,
            ) from exc
        session.commit()
        workflow = session.get(WorkflowExecution, workflow_id)
        if workflow is None:
            if not result.requires_persisted_execution:
                return HandlerWorkflowAdvanceResult(
                    handled=result.handled,
                    reason=result.reason,
                    status="ignored",
                    active_run_id=None,
                    last_error=None,
                )
            raise RuntimeError(f"Workflow advance did not persist workflow execution {workflow_id}")
        status_payload = _workflow_status_payload(workflow=workflow)
        return HandlerWorkflowAdvanceResult(
            handled=result.handled,
            reason=result.reason,
            status=status_payload["status"] or "running",
            active_run_id=status_payload["active_run_id"],
            last_error=status_payload["last_error"],
        )


@activity.defn(name="retry_handler_workflow_operation_activity")
def retry_handler_workflow_operation_activity(payload: WorkflowOperationRetryInput) -> WorkflowOperationRetryResult:
    settings = get_settings()
    session_factory = create_session_factory()
    with session_factory() as session:
        workflow = session.get(WorkflowExecution, str(payload.workflow_id or "").strip())
        if workflow is None:
            raise RuntimeError(f"Workflow retry is missing workflow {payload.workflow_id}")
        operation = session.get(WorkflowOperation, str(payload.operation_id or "").strip())
        if operation is None or operation.workflow_id != workflow.workflow_id:
            raise RuntimeError(f"Workflow retry is missing operation {payload.operation_id}")
        handler_registry = build_runtime_workflow_handler_registry()
        try:
            handle = retry_workflow_operation_with_registered_handler(
                session=session,
                settings=settings,
                session_factory=session_factory,
                workflow=workflow,
                operation=operation,
                handler_registry=handler_registry,
            )
        except RetryableSpecialistPlanningContractError as exc:
            session.commit()
            raise ApplicationError(
                str(exc),
                type="retryable_invalid_model_output",
            ) from exc
        except (
            InvalidWorkflowOperationRetryError,
            UnsupportedWorkflowOperationRetryError,
            WorkflowOperationAttemptAlreadyRunningError,
        ) as exc:
            raise ApplicationError(
                str(exc),
                type="terminal_workflow_operation_retry_error",
                non_retryable=True,
            ) from exc
        session.commit()
        refreshed_workflow = session.get(WorkflowExecution, workflow.workflow_id)
        if refreshed_workflow is None:
            raise RuntimeError(f"Workflow retry lost workflow {workflow.workflow_id}")
        status_payload = _workflow_status_payload(workflow=refreshed_workflow)
        return WorkflowOperationRetryResult(
            operation_id=handle.operation_id,
            workflow_id=handle.workflow_id,
            operation_type=handle.operation_type,
            operation_status=handle.status,
            workflow_status=status_payload["status"] or "running",
            active_run_id=status_payload["active_run_id"],
            last_error=status_payload["last_error"],
        )
