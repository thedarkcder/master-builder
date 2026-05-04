from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator.core.parent_feature_workflow.child_fanout_execution import (
    ChildFanoutExecutionError,
    ChildFanoutExecutionInput,
    execute_child_fanout_step,
)
from orchestrator.core.projects.parent_planning_fanout_service import ParentPlanningFanoutService
from orchestrator.core.planning.specialist import PLANNING_STATE_COMPLETED
from orchestrator.core.runtime.payload_models import PMDecisionRequestPayload
from orchestrator.core.workflow.step_runner import WorkflowStepAttempt


class _ChildSyncGateway:
    def __init__(self, *, seed_data: dict[str, object]) -> None:
        self.seed_data = seed_data
        self.seed_calls = 0

    def seed_parent_backlog_children(self, **_kwargs):  # noqa: ANN003
        self.seed_calls += 1
        return self.seed_data

    def combined_child_updates(self, *, seed_data: dict[str, object]):
        return (
            list(seed_data.get("updated_children", [])),
            list(seed_data.get("created_children", [])),
            list(seed_data.get("changed_children", [])),
        )


class _AttemptAwareChildSyncGateway(_ChildSyncGateway):
    def __init__(self, *, seed_data: dict[str, object]) -> None:
        super().__init__(seed_data=seed_data)
        self.attempt_refs = []

    def with_attempt(self, *, attempt_ref):  # noqa: ANN001
        self.attempt_refs.append(attempt_ref)
        return self


class _Lifecycle:
    def __init__(self) -> None:
        self.completed = []
        self.failed = []

    def complete_started_operation(self, *, operation, attempt, summary):  # noqa: ANN001
        self.completed.append((operation, attempt, summary))

    def fail_started_operation(self, *, operation, attempt, category, message):  # noqa: ANN001
        self.failed.append((operation, attempt, category, message))


def _step_attempt() -> WorkflowStepAttempt:
    return WorkflowStepAttempt(
        operation=SimpleNamespace(workflow_id="workflow-1", operation_id="operation-1"),
        attempt=SimpleNamespace(attempt_id="attempt-1", attempt_number=1),
    )


def test_parent_planning_fanout_service_requires_questions_for_blocked_result() -> None:
    with pytest.raises(RuntimeError, match="blocked but did not return clarification questions"):
        ParentPlanningFanoutService().blocked_planning_result(
            planning_result=SimpleNamespace(planning_state="planning_needs_clarification", pm_decision_requests=()),
            planning_package={"child_ticket_specs": []},
        )


def test_parent_planning_fanout_service_returns_blocking_questions() -> None:
    result = ParentPlanningFanoutService().blocked_planning_result(
        planning_result=SimpleNamespace(
            planning_state="planning_needs_clarification",
            pm_decision_requests=[
                PMDecisionRequestPayload(
                    request_id="pm-audit-retention",
                    question="What audit retention window should v1 support?",
                    why_it_matters="The answer changes product commitments.",
                    related_decision_ids=("audit-retention",),
                )
            ],
        ),
        planning_package={"child_ticket_specs": []},
    )

    assert result.completed is False
    assert result.changed_children == []
    assert result.questions[0].question == "What audit retention window should v1 support?"


def test_parent_planning_fanout_service_evaluates_refresh_seed_data() -> None:
    result = ParentPlanningFanoutService().evaluate_seed_data(
        seed_data={
            "requires_input": True,
            "questions": ["Which child behavior should update?"],
            "updated_children": ["MAB-230"],
            "created_children": [],
            "changed_children": ["MAB-230"],
        },
        combine_child_updates_fn=_ChildSyncGateway(seed_data={}).combined_child_updates,
    )

    assert result.completed is False
    assert result.changed_children == ["MAB-230"]
    assert result.questions[0].question == "Which child behavior should update?"


def test_child_fanout_executor_uses_started_attempt_contract() -> None:
    lifecycle = _Lifecycle()
    gateway = _AttemptAwareChildSyncGateway(
        seed_data={
            "requires_input": False,
            "updated_children": ["MAB-230"],
            "created_children": [],
            "changed_children": ["MAB-230"],
        }
    )

    result = execute_child_fanout_step(
        request=ChildFanoutExecutionInput(
            lifecycle=lifecycle,
            step=_step_attempt(),
            child_sync_gateway=gateway,
            fanout_service=ParentPlanningFanoutService(),
            parent_detail=SimpleNamespace(key="MAB-229"),
            project_key="MAB",
            planning_result=SimpleNamespace(planning_state=PLANNING_STATE_COMPLETED, pm_decision_requests=()),
            planning_package={"planning_state": PLANNING_STATE_COMPLETED},
            completion_summary="Fanout complete.",
        )
    )

    assert result.completed is True
    assert gateway.seed_calls == 1
    assert gateway.attempt_refs[0].attempt_id == "attempt-1"
    assert lifecycle.completed[0][2] == "Fanout complete."


def test_child_fanout_executor_fails_attempt_when_seed_contract_is_invalid() -> None:
    lifecycle = _Lifecycle()
    gateway = _AttemptAwareChildSyncGateway(seed_data={"requires_input": True, "questions": []})

    with pytest.raises(ChildFanoutExecutionError, match="blocked but did not return clarification questions"):
        execute_child_fanout_step(
            request=ChildFanoutExecutionInput(
                lifecycle=lifecycle,
                step=_step_attempt(),
                child_sync_gateway=gateway,
                fanout_service=ParentPlanningFanoutService(),
                parent_detail=SimpleNamespace(key="MAB-229"),
                project_key="MAB",
                planning_result=SimpleNamespace(planning_state=PLANNING_STATE_COMPLETED, pm_decision_requests=()),
                planning_package={"planning_state": PLANNING_STATE_COMPLETED},
                completion_summary="Fanout complete.",
            )
        )

    assert lifecycle.failed[0][2] == "contract_violation"
