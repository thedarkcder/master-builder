from __future__ import annotations

from sqlalchemy.orm import Session, sessionmaker

from orchestrator.core.config import Settings
from orchestrator.core.workflow.advance import execute_workflow_operation_retry
from orchestrator.core.workflow.handler_registry import WorkflowHandlerRegistry
from orchestrator.core.workflow.operation_service import WorkflowOperationHandle
from orchestrator.storage.models import WorkflowExecution, WorkflowOperation


def retry_workflow_operation_with_registered_handler(
    *,
    session: Session,
    settings: Settings,
    session_factory: sessionmaker[Session],
    workflow: WorkflowExecution,
    operation: WorkflowOperation,
    handler_registry: WorkflowHandlerRegistry,
) -> WorkflowOperationHandle:
    return execute_workflow_operation_retry(
        session=session,
        settings=settings,
        session_factory=session_factory,
        workflow=workflow,
        operation=operation,
        resolve_operation_retry_handler_fn=handler_registry.resolve_operation_retry_handler,
    )
