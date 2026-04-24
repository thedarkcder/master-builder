from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from sqlalchemy.orm import Session, sessionmaker

from orchestrator.core.config import Settings
from orchestrator.core.workflow_operation_service import WorkflowOperationHandle
from orchestrator.core.workflow_type_catalog import get_workflow_type
from orchestrator.core.workflow_execution_projection import (
    WorkflowExecutionProjection,
    WorkflowExecutionReference,
    ensure_workflow_execution,
)
from orchestrator.storage.models import WorkflowExecution, WorkflowOperation


@dataclass(frozen=True)
class WorkflowTrigger:
    event: str | None = None
    command: str | None = None
    argument: str | None = None


@dataclass(frozen=True)
class WorkflowAdvanceRequest:
    workflow_handler_key: str
    tenant_id: str
    tenant: Any
    execution: WorkflowExecutionReference
    project_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    trigger: WorkflowTrigger = field(default_factory=WorkflowTrigger)


@dataclass(frozen=True)
class WorkflowAdvanceOutcome:
    handled: bool
    reason: str | None = None
    extra: dict[str, object] = field(default_factory=dict)


class WorkflowAdvanceLifecycle(Protocol):
    def ensure_execution(self, *, display_name: str | None, description: object | None) -> None:
        ...

    def mark_running(self) -> None:
        ...

    def set_operation_completed(self, *, operation_type: str, summary: str) -> None:
        ...

    def mark_operation_completed(self, *, operation_type: str, summary: str) -> None:
        ...

    def mark_operation_failed(
        self,
        *,
        operation_type: str,
        category: str,
        message: str,
    ) -> None:
        ...

    def set_operation_waiting_for_input(self, *, operation_type: str, summary: str) -> None:
        ...

    def mark_waiting_for_input(self, *, operation_type: str, summary: str) -> None:
        ...

    def mark_completed_if_ready(self) -> None:
        ...


class DurableWorkflowLifecycle:
    def __init__(
        self,
        *,
        session: Session,
        workflow_type: Any,
        tenant_id: str,
        project_id: str | None,
        execution: WorkflowExecutionReference,
    ) -> None:
        self._session = session
        self._workflow_type = workflow_type
        self._tenant_id = tenant_id
        self._project_id = project_id
        self._execution = execution
        self._display_name: str | None = execution.source.display_name
        self._description: object | None = execution.source.description
        self._projection: WorkflowExecutionProjection | None = None

    def _ensure_projection(self) -> WorkflowExecutionProjection:
        if self._projection is None:
            self._projection = ensure_workflow_execution(
                session=self._session,
                workflow_type=self._workflow_type,
                tenant_id=self._tenant_id,
                project_id=self._project_id,
                execution=self._execution,
                display_name=self._display_name,
                description=self._description,
            )
        return self._projection

    def ensure_execution(self, *, display_name: str | None, description: object | None) -> None:
        self._display_name = display_name
        self._description = description
        self._projection = ensure_workflow_execution(
            session=self._session,
            workflow_type=self._workflow_type,
            tenant_id=self._tenant_id,
            project_id=self._project_id,
            execution=self._execution,
            display_name=display_name,
            description=description,
        )

    def mark_running(self) -> None:
        self._ensure_projection().mark_running()

    def set_operation_completed(self, *, operation_type: str, summary: str) -> None:
        self._ensure_projection().set_operation_completed(operation_type=operation_type, summary=summary)

    def mark_operation_completed(self, *, operation_type: str, summary: str) -> None:
        self._ensure_projection().mark_operation_completed(operation_type=operation_type, summary=summary)

    def mark_operation_failed(
        self,
        *,
        operation_type: str,
        category: str,
        message: str,
    ) -> None:
        self._ensure_projection().mark_operation_failed(
            operation_type=operation_type,
            category=category,
            message=message,
        )

    def set_operation_waiting_for_input(self, *, operation_type: str, summary: str) -> None:
        self._ensure_projection().set_operation_waiting_for_input(operation_type=operation_type, summary=summary)

    def mark_waiting_for_input(self, *, operation_type: str, summary: str) -> None:
        self._ensure_projection().mark_waiting_for_input(operation_type=operation_type, summary=summary)

    def mark_completed_if_ready(self) -> None:
        self._ensure_projection().mark_completed_if_ready()


class WorkflowAdvanceHandler(Protocol):
    def advance(
        self,
        *,
        session: Session,
        settings: Settings,
        workflow_type,
        request: WorkflowAdvanceRequest,
        lifecycle: WorkflowAdvanceLifecycle,
    ) -> WorkflowAdvanceOutcome:
        ...


class WorkflowOperationRetryHandler(Protocol):
    def retry_operation(
        self,
        *,
        session: Session,
        settings: Settings,
        session_factory: sessionmaker[Session],
        workflow_type,
        workflow: WorkflowExecution,
        operation: WorkflowOperation,
    ) -> WorkflowOperationHandle:
        ...


class UnsupportedWorkflowOperationRetryError(RuntimeError):
    pass


class InvalidWorkflowOperationRetryError(RuntimeError):
    pass


def execute_workflow_advance(
    *,
    session: Session,
    settings: Settings,
    workflow_type,
    request: WorkflowAdvanceRequest,
    resolve_advance_handler_fn: Callable[[str], WorkflowAdvanceHandler],
) -> WorkflowAdvanceOutcome:
    handler = resolve_advance_handler_fn(str(workflow_type.handler_key or "").strip())
    lifecycle = DurableWorkflowLifecycle(
        session=session,
        workflow_type=workflow_type,
        tenant_id=request.tenant_id,
        project_id=request.project_id,
        execution=request.execution,
    )
    return handler.advance(
        session=session,
        settings=settings,
        workflow_type=workflow_type,
        request=request,
        lifecycle=lifecycle,
    )


def execute_workflow_operation_retry(
    *,
    session: Session,
    settings: Settings,
    session_factory: sessionmaker[Session],
    workflow: WorkflowExecution,
    operation: WorkflowOperation,
    resolve_operation_retry_handler_fn: Callable[[str], WorkflowOperationRetryHandler],
) -> WorkflowOperationHandle:
    workflow_type = get_workflow_type(session, workflow_type_key=workflow.workflow_type_key)
    handler = resolve_operation_retry_handler_fn(str(workflow_type.handler_key or "").strip())
    return handler.retry_operation(
        session=session,
        settings=settings,
        session_factory=session_factory,
        workflow_type=workflow_type,
        workflow=workflow,
        operation=operation,
    )
