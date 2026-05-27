from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from sqlalchemy.orm import Session, sessionmaker

from orchestrator.core.config import Settings
from orchestrator.core.workflow.operation_service import WorkflowOperationHandle
from orchestrator.core.workflow.type_catalog import get_workflow_type
from orchestrator.core.workflow.execution_projection import (
    WorkflowExecutionProjection,
    WorkflowExecutionReference,
    ensure_workflow_execution,
    workflow_execution_id,
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
    requires_persisted_execution: bool = True
    failed: bool = False


@dataclass(frozen=True)
class WorkflowOperationRetryCapability:
    operation_type: str


@dataclass(frozen=True)
class WorkflowOperationRetryRequest:
    session: Session
    settings: Settings
    session_factory: sessionmaker[Session]
    workflow_type: Any
    workflow: WorkflowExecution
    operation: WorkflowOperation


class WorkflowAdvanceLifecycle(Protocol):
    @property
    def workflow_type(self):
        ...

    def has_execution(self) -> bool:
        ...

    def ensure_execution(self, *, display_name: str | None, description: object | None) -> None:
        ...

    def mark_running(self) -> None:
        ...

    def start_operation_attempt(
        self,
        *,
        operation_type: str,
        run_id: str | None = None,
        idempotency_key: str | None = None,
        target_system: str | None = None,
        target_ref: str | None = None,
        summary: str | None = None,
    ):
        ...

    def complete_started_operation(self, *, operation, attempt, summary: str) -> None:  # noqa: ANN001
        ...

    def fail_started_operation(
        self,
        *,
        operation,  # noqa: ANN001
        attempt,  # noqa: ANN001
        category: str,
        message: str,
    ) -> None:
        ...

    def wait_started_operation(self, *, operation, attempt, summary: str) -> None:  # noqa: ANN001
        ...

    def complete_waiting_operation_attempt(self, *, operation_type: str, summary: str):  # noqa: ANN001
        ...

    def mark_workflow_waiting_for_input(self) -> None:
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

    @property
    def workflow_type(self):
        return self._workflow_type

    @property
    def session(self) -> Session:
        return self._session

    @property
    def workflow(self) -> WorkflowExecution:
        return self._ensure_projection().workflow

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

    def has_execution(self) -> bool:
        workflow_id = workflow_execution_id(
            workflow_type_key=self._workflow_type.workflow_type_key,
            execution_key=self._execution.key,
        )
        return self._session.get(WorkflowExecution, workflow_id) is not None

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

    def start_operation_attempt(
        self,
        *,
        operation_type: str,
        run_id: str | None = None,
        idempotency_key: str | None = None,
        target_system: str | None = None,
        target_ref: str | None = None,
        summary: str | None = None,
    ):
        operation, attempt = self._ensure_projection().start_operation_attempt(
            operation_type=operation_type,
            run_id=run_id,
            idempotency_key=idempotency_key,
            target_system=target_system,
            target_ref=target_ref,
            summary=summary,
        )
        self._session.commit()
        return operation, attempt

    def complete_started_operation(self, *, operation, attempt, summary: str) -> None:  # noqa: ANN001
        self._ensure_projection().complete_started_operation(operation=operation, attempt=attempt, summary=summary)
        self._session.commit()

    def fail_started_operation(
        self,
        *,
        operation,  # noqa: ANN001
        attempt,  # noqa: ANN001
        category: str,
        message: str,
    ) -> None:
        self._ensure_projection().fail_started_operation(
            operation=operation,
            attempt=attempt,
            category=category,
            message=message,
        )
        self._session.commit()

    def wait_started_operation(self, *, operation, attempt, summary: str) -> None:  # noqa: ANN001
        self._ensure_projection().wait_started_operation(operation=operation, attempt=attempt, summary=summary)
        self._session.commit()

    def complete_waiting_operation_attempt(self, *, operation_type: str, summary: str):  # noqa: ANN001
        result = self._ensure_projection().complete_waiting_operation_attempt(
            operation_type=operation_type,
            summary=summary,
        )
        self._session.commit()
        return result

    def mark_workflow_waiting_for_input(self) -> None:
        self._ensure_projection().mark_workflow_waiting_for_input()
        self._session.commit()

    def mark_completed_if_ready(self) -> None:
        self._ensure_projection().mark_completed_if_ready()
        self._session.commit()


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
    def operation_retry_capabilities(self, workflow_type) -> tuple[WorkflowOperationRetryCapability, ...]:  # noqa: ANN001
        ...

    def retry_operation(
        self,
        *,
        request: WorkflowOperationRetryRequest,
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
        request=WorkflowOperationRetryRequest(
            session=session,
            settings=settings,
            session_factory=session_factory,
            workflow_type=workflow_type,
            workflow=workflow,
            operation=operation,
        )
    )
