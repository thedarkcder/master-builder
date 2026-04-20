from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from sqlalchemy.orm import Session

from orchestrator.core.config import Settings
from orchestrator.core.workflow_execution_projection import ensure_issue_workflow_execution


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
class WorkflowTransitionStep:
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkflowTransitionPlan:
    steps: tuple[WorkflowTransitionStep, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.steps)


@dataclass(frozen=True)
class WorkflowAdvanceOutcome:
    handled: bool
    reason: str | None = None
    extra: dict[str, object] = field(default_factory=dict)
    transition_plan: WorkflowTransitionPlan | None = None


class WorkflowAdvanceLifecycle(Protocol):
    def ensure_issue_execution(self, *, issue_summary: str | None, issue_description: object | None) -> None:
        ...

    def mark_running(self) -> None:
        ...

    def mark_operation_completed(self, *, operation_type: str, summary: str) -> None:
        ...

    def mark_operation_failed(
        self,
        *,
        operation_type: str,
        category: str,
        message: str,
        retryable: bool,
    ) -> None:
        ...

    def mark_waiting_for_input(self, *, operation_type: str, summary: str) -> None:
        ...

    def mark_completed_if_ready(self) -> None:
        ...


@dataclass
class WorkflowTransitionPlanner:
    _steps: list[WorkflowTransitionStep] = field(default_factory=list)

    def ensure_issue_execution(self, *, issue_summary: str | None, issue_description: object | None) -> None:
        self._steps.append(
            WorkflowTransitionStep(
                kind="ensure_issue_execution",
                payload={
                    "issue_summary": issue_summary,
                    "issue_description": issue_description,
                },
            )
        )

    def mark_running(self) -> None:
        self._steps.append(WorkflowTransitionStep(kind="mark_running"))

    def mark_operation_completed(self, *, operation_type: str, summary: str) -> None:
        self._steps.append(
            WorkflowTransitionStep(
                kind="mark_operation_completed",
                payload={
                    "operation_type": operation_type,
                    "summary": summary,
                },
            )
        )

    def mark_operation_failed(
        self,
        *,
        operation_type: str,
        category: str,
        message: str,
        retryable: bool,
    ) -> None:
        self._steps.append(
            WorkflowTransitionStep(
                kind="mark_operation_failed",
                payload={
                    "operation_type": operation_type,
                    "category": category,
                    "message": message,
                    "retryable": retryable,
                },
            )
        )

    def mark_waiting_for_input(self, *, operation_type: str, summary: str) -> None:
        self._steps.append(
            WorkflowTransitionStep(
                kind="mark_waiting_for_input",
                payload={
                    "operation_type": operation_type,
                    "summary": summary,
                },
            )
        )

    def mark_completed_if_ready(self) -> None:
        self._steps.append(WorkflowTransitionStep(kind="mark_completed_if_ready"))

    def build_outcome(
        self,
        *,
        handled: bool,
        reason: str | None = None,
        extra: dict[str, object] | None = None,
    ) -> WorkflowAdvanceOutcome:
        return WorkflowAdvanceOutcome(
            handled=handled,
            reason=reason,
            extra=dict(extra or {}),
            transition_plan=WorkflowTransitionPlan(steps=tuple(self._steps)),
        )


class WorkflowAdvanceHandler(Protocol):
    def advance(
        self,
        *,
        session: Session,
        settings: Settings,
        workflow_type,
        request: WorkflowAdvanceRequest,
    ) -> WorkflowAdvanceOutcome:
        ...


def apply_workflow_transition_plan(
    *,
    session: Session,
    workflow_type: Any,
    tenant_id: str,
    project_id: str | None,
    issue_key: str,
    transition_plan: WorkflowTransitionPlan | None,
) -> None:
    if not transition_plan:
        return
    projection = None
    issue_summary = None
    issue_description = None
    for step in transition_plan.steps:
        if step.kind == "ensure_issue_execution":
            issue_summary = step.payload.get("issue_summary")
            issue_description = step.payload.get("issue_description")
            projection = ensure_issue_workflow_execution(
                session=session,
                workflow_type=workflow_type,
                tenant_id=tenant_id,
                project_id=project_id,
                issue_key=issue_key,
                issue_summary=issue_summary,
                issue_description=issue_description,
            )
            continue
        if projection is None:
            projection = ensure_issue_workflow_execution(
                session=session,
                workflow_type=workflow_type,
                tenant_id=tenant_id,
                project_id=project_id,
                issue_key=issue_key,
                issue_summary=issue_summary,
                issue_description=issue_description,
            )
        if step.kind == "mark_running":
            projection.mark_running()
        elif step.kind == "mark_operation_completed":
            projection.mark_operation_completed(**step.payload)
        elif step.kind == "mark_operation_failed":
            projection.mark_operation_failed(**step.payload)
        elif step.kind == "mark_waiting_for_input":
            projection.mark_waiting_for_input(**step.payload)
        elif step.kind == "mark_completed_if_ready":
            projection.mark_completed_if_ready()
        else:  # pragma: no cover
            raise RuntimeError(f"Unsupported workflow transition step kind: {step.kind}")


def execute_workflow_advance(
    *,
    session: Session,
    settings: Settings,
    workflow_type,
    request: WorkflowAdvanceRequest,
    resolve_advance_handler_fn: Callable[[str], WorkflowAdvanceHandler],
) -> WorkflowAdvanceOutcome:
    handler = resolve_advance_handler_fn(str(workflow_type.handler_key or "").strip())
    result = handler.advance(
        session=session,
        settings=settings,
        workflow_type=workflow_type,
        request=request,
    )
    apply_workflow_transition_plan(
        session=session,
        workflow_type=workflow_type,
        tenant_id=request.tenant_id,
        project_id=request.project_id,
        issue_key=request.issue_key,
        transition_plan=result.transition_plan,
    )
    return result
