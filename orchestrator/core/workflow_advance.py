from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from sqlalchemy.orm import Session

from orchestrator.core.config import Settings
from orchestrator.core.workflow_execution_projection import (
    WorkflowExecutionProjection,
    ensure_issue_workflow_execution,
)


@dataclass(frozen=True)
class WorkflowAdvanceRequest:
    workflow_handler_key: str
    tenant_id: str
    tenant: Any
    issue_key: str
    project_id: str | None = None
    issue_summary: str | None = None
    issue_description: object | None = None
    issue_labels: tuple[str, ...] = ()
    payload: dict[str, Any] = field(default_factory=dict)
    webhook_event: str | None = None
    comment_command: str | None = None
    comment_command_argument: str | None = None


@dataclass(frozen=True)
class WorkflowAdvanceOutcome:
    handled: bool
    reason: str | None = None
    extra: dict[str, object] = field(default_factory=dict)


class WorkflowAdvanceLifecycle(Protocol):
    def ensure_issue_execution(self, *, issue_summary: str | None, issue_description: object | None) -> None:
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
        issue_key: str,
    ) -> None:
        self._session = session
        self._workflow_type = workflow_type
        self._tenant_id = tenant_id
        self._project_id = project_id
        self._issue_key = issue_key
        self._issue_summary: str | None = None
        self._issue_description: object | None = None
        self._projection: WorkflowExecutionProjection | None = None

    def _ensure_projection(self) -> WorkflowExecutionProjection:
        if self._projection is None:
            self._projection = ensure_issue_workflow_execution(
                session=self._session,
                workflow_type=self._workflow_type,
                tenant_id=self._tenant_id,
                project_id=self._project_id,
                issue_key=self._issue_key,
                issue_summary=self._issue_summary,
                issue_description=self._issue_description,
            )
        return self._projection

    def ensure_issue_execution(self, *, issue_summary: str | None, issue_description: object | None) -> None:
        self._issue_summary = issue_summary
        self._issue_description = issue_description
        self._projection = ensure_issue_workflow_execution(
            session=self._session,
            workflow_type=self._workflow_type,
            tenant_id=self._tenant_id,
            project_id=self._project_id,
            issue_key=self._issue_key,
            issue_summary=issue_summary,
            issue_description=issue_description,
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
        issue_key=request.issue_key,
    )
    return handler.advance(
        session=session,
        settings=settings,
        workflow_type=workflow_type,
        request=request,
        lifecycle=lifecycle,
    )
