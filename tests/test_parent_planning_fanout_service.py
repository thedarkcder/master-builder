from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator.core.parent_planning_fanout_service import ParentPlanningFanoutService
from orchestrator.core.specialist_planning import PLANNING_STATE_COMPLETED


class _Planner:
    def __init__(self, *, planning_result, planning_package: dict[str, object]) -> None:
        self.planning_result = planning_result
        self.planning_package = planning_package

    def plan_backlog_parent(self, **_kwargs):  # noqa: ANN003
        return self.planning_result, self.planning_package


class _ChildSyncGateway:
    def __init__(self, *, seed_data: dict[str, object]) -> None:
        self.seed_data = seed_data

    def seed_parent_backlog_children(self, **_kwargs):  # noqa: ANN003
        return self.seed_data

    def combined_child_updates(self, *, seed_data: dict[str, object]):
        return (
            list(seed_data.get("updated_children", [])),
            list(seed_data.get("created_children", [])),
            list(seed_data.get("changed_children", [])),
        )


def test_parent_planning_fanout_service_returns_completed_result() -> None:
    result = ParentPlanningFanoutService().plan_and_seed(
        parent_detail=SimpleNamespace(key="MAB-229"),
        product_brief={"objective": "Create child tickets"},
        project_key="MAB",
        planner=_Planner(
            planning_result=SimpleNamespace(planning_state=PLANNING_STATE_COMPLETED, open_behavior_questions=()),
            planning_package={"child_ticket_specs": []},
        ),
        child_sync_gateway=_ChildSyncGateway(
            seed_data={
                "requires_input": False,
                "updated_children": ["MAB-230"],
                "created_children": ["MAB-231"],
                "changed_children": ["MAB-230", "MAB-231"],
            }
        ),
    )

    assert result.completed is True
    assert result.changed_children == ["MAB-230", "MAB-231"]
    assert result.questions == ()


def test_parent_planning_fanout_service_requires_questions_for_blocked_result() -> None:
    with pytest.raises(RuntimeError, match="blocked but did not return clarification questions"):
        ParentPlanningFanoutService().plan_and_seed(
            parent_detail=SimpleNamespace(key="MAB-229"),
            product_brief={"objective": "Create child tickets"},
            project_key="MAB",
            planner=_Planner(
                planning_result=SimpleNamespace(planning_state="planning_needs_clarification", open_behavior_questions=()),
                planning_package={"child_ticket_specs": []},
            ),
            child_sync_gateway=_ChildSyncGateway(seed_data={"requires_input": True, "questions": []}),
        )


def test_parent_planning_fanout_service_returns_blocking_questions() -> None:
    result = ParentPlanningFanoutService().plan_and_seed(
        parent_detail=SimpleNamespace(key="MAB-229"),
        product_brief={"objective": "Create child tickets"},
        project_key="MAB",
        planner=_Planner(
            planning_result=SimpleNamespace(
                planning_state="planning_needs_clarification",
                open_behavior_questions=[
                    {
                        "question": "What audit retention window should v1 support?",
                        "kind": "product",
                    }
                ],
            ),
            planning_package={"child_ticket_specs": []},
        ),
        child_sync_gateway=_ChildSyncGateway(seed_data={"requires_input": True, "questions": []}),
    )

    assert result.completed is False
    assert result.questions[0].question == "What audit retention window should v1 support?"
