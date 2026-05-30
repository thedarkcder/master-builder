from __future__ import annotations

from dataclasses import dataclass

from orchestrator.core.workflow.attempt_ref import WorkflowAttemptRef
from orchestrator.storage.models import WorkflowOperation, WorkflowOperationAttempt


@dataclass(frozen=True)
class WorkflowStepAttempt:
    operation: WorkflowOperation
    attempt: WorkflowOperationAttempt

    @property
    def ref(self) -> WorkflowAttemptRef:
        return WorkflowAttemptRef(
            workflow_id=self.operation.workflow_id,
            operation_id=self.operation.operation_id,
            attempt_id=self.attempt.attempt_id,
            number=self.attempt.attempt_number,
        )

    @property
    def workflow_id(self) -> str:
        return self.operation.workflow_id

    @property
    def operation_id(self) -> str:
        return self.operation.operation_id

    @property
    def attempt_number(self) -> int:
        return self.attempt.attempt_number

    @property
    def attempt_id(self) -> str:
        return self.attempt.attempt_id


def start_workflow_step_attempt(
    *,
    lifecycle,  # noqa: ANN001
    operation_type: str,
    run_id: str | None = None,
    idempotency_key: str | None = None,
    target_system: str | None = None,
    target_ref: str | None = None,
    summary: str | None = None,
) -> WorkflowStepAttempt:
    workflow_type = getattr(lifecycle, "workflow_type", None)
    if workflow_type is None or not hasattr(workflow_type, "step"):
        raise ValueError("Workflow step attempts require a code-defined workflow definition")
    workflow_type.step(operation_type)
    operation, attempt = lifecycle.start_operation_attempt(
        operation_type=operation_type,
        run_id=run_id,
        idempotency_key=idempotency_key,
        target_system=target_system,
        target_ref=target_ref,
        summary=summary,
    )
    return WorkflowStepAttempt(operation=operation, attempt=attempt)


def complete_workflow_step_attempt(*, lifecycle, step: WorkflowStepAttempt, summary: str) -> None:  # noqa: ANN001
    lifecycle.complete_started_operation(operation=step.operation, attempt=step.attempt, summary=summary)


def fail_workflow_step_attempt(
    *,
    lifecycle,  # noqa: ANN001
    step: WorkflowStepAttempt,
    category: str,
    message: str,
) -> None:
    lifecycle.fail_started_operation(
        operation=step.operation,
        attempt=step.attempt,
        category=category,
        message=message,
    )


def retry_workflow_step_attempt(
    *,
    lifecycle,  # noqa: ANN001
    step: WorkflowStepAttempt,
    category: str,
    message: str,
) -> None:
    lifecycle.retry_started_operation(
        operation=step.operation,
        attempt=step.attempt,
        category=category,
        message=message,
    )


def wait_workflow_step_attempt(*, lifecycle, step: WorkflowStepAttempt, summary: str) -> None:  # noqa: ANN001
    lifecycle.wait_started_operation(operation=step.operation, attempt=step.attempt, summary=summary)


def complete_waiting_workflow_step_attempt(
    *,
    lifecycle,  # noqa: ANN001
    operation_type: str,
    summary: str,
) -> WorkflowStepAttempt:
    workflow_type = getattr(lifecycle, "workflow_type", None)
    if workflow_type is None or not hasattr(workflow_type, "step"):
        raise ValueError("Workflow step attempts require a code-defined workflow definition")
    workflow_type.step(operation_type)
    operation, attempt = lifecycle.complete_waiting_operation_attempt(
        operation_type=operation_type,
        summary=summary,
    )
    return WorkflowStepAttempt(operation=operation, attempt=attempt)
