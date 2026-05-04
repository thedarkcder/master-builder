from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from orchestrator.core.projects.parent_planning_fanout_service import (
    ParentPlanningFanoutResult,
    ParentPlanningFanoutSeedError,
    ParentPlanningFanoutService,
)
from orchestrator.core.parent_feature_workflow.operations import PARENT_WU_JIRA_CHILD_FANOUT_EVALUATE
from orchestrator.core.workflow.attempt_ref import WorkflowAttemptRef
from orchestrator.core.workflow.execution_projection import classify_external_workflow_failure
from orchestrator.core.workflow.step_runner import (
    WorkflowStepAttempt,
    complete_workflow_step_attempt,
    fail_workflow_step_attempt,
)
from orchestrator.core.workflow.work_units import run_work_unit, workflow_work_unit_input_fingerprint


class ChildFanoutExecutionError(RuntimeError):
    pass


class ChildFanoutGateway(Protocol):
    def with_attempt(self, *, attempt_ref: WorkflowAttemptRef) -> ChildFanoutGateway: ...

    def seed_parent_backlog_children(
        self,
        *,
        parent_detail: object,
        project_key: str,
        planning_package: dict[str, Any],
        planning_state: str,
    ) -> dict[str, Any]: ...

    def combined_child_updates(self, *, seed_data: dict[str, Any]) -> tuple[list[str], list[str], list[str]]: ...


@dataclass(frozen=True)
class ChildFanoutExecutionInput:
    lifecycle: object
    step: WorkflowStepAttempt
    child_sync_gateway: ChildFanoutGateway
    fanout_service: ParentPlanningFanoutService
    parent_detail: object
    project_key: str
    planning_result: object
    planning_package: dict[str, Any]
    completion_summary: str


def execute_child_fanout_step(*, request: ChildFanoutExecutionInput) -> ParentPlanningFanoutResult:
    child_sync_gateway = request.child_sync_gateway.with_attempt(attempt_ref=request.step.ref)
    try:
        seed_data = child_sync_gateway.seed_parent_backlog_children(
            parent_detail=request.parent_detail,
            project_key=request.project_key,
            planning_package=request.planning_package,
            planning_state=request.planning_result.planning_state,
        )
    except Exception as exc:  # noqa: BLE001
        fail_workflow_step_attempt(
            lifecycle=request.lifecycle,
            step=request.step,
            category=classify_external_workflow_failure(error=exc),
            message=str(exc),
        )
        raise ParentPlanningFanoutSeedError(
            error=exc,
            planning_result=request.planning_result,
            planning_package=request.planning_package,
        ) from exc

    try:
        evaluation_input = {
            "seed_data": seed_data,
            "planning_state": request.planning_result.planning_state,
            "planning_package": request.planning_package,
        }
        evaluation_input_hash = workflow_work_unit_input_fingerprint(evaluation_input)
        seed_evaluation = run_work_unit(
            request.lifecycle.session,
            operation=request.step.operation,
            operation_attempt=request.step.attempt,
            unit_key=PARENT_WU_JIRA_CHILD_FANOUT_EVALUATE,
            idempotency_key=f"{getattr(request.parent_detail, 'key', '')}:evaluate_child_fanout:{evaluation_input_hash}",
            input_payload=evaluation_input,
            execute=lambda _context: request.fanout_service.evaluate_seed_data(
                seed_data=seed_data,
                combine_child_updates_fn=child_sync_gateway.combined_child_updates,
                planning_result=request.planning_result,
            ),
            serialize=lambda result: {"seed_evaluation": result.to_payload()},
            deserialize=lambda payload: request.fanout_service.seed_evaluation_from_payload(
                payload.get("seed_evaluation")
            ),
        )
        fanout = ParentPlanningFanoutResult(
            planning_result=request.planning_result,
            planning_package=request.planning_package,
            seed_evaluation=seed_evaluation,
        )
    except Exception as exc:  # noqa: BLE001
        fail_workflow_step_attempt(
            lifecycle=request.lifecycle,
            step=request.step,
            category="contract_violation",
            message=str(exc),
        )
        raise ChildFanoutExecutionError(str(exc)) from exc
    if fanout.completed:
        complete_workflow_step_attempt(
            lifecycle=request.lifecycle,
            step=request.step,
            summary=request.completion_summary,
        )
        return fanout

    fail_workflow_step_attempt(
        lifecycle=request.lifecycle,
        step=request.step,
        category="invalid_model_output",
        message="Engineering child fanout returned clarification questions after PM decision resolution.",
    )
    raise ChildFanoutExecutionError("Engineering child fanout returned clarification questions after PM decision resolution.")
