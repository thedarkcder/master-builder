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


def test_parent_planning_fanout_service_returns_completed_result() -> None:
    result = ParentPlanningFanoutService().plan_and_seed(
        parent_detail=SimpleNamespace(key="MAB-229"),
        product_brief={"objective": "Create child tickets"},
        project_key="MAB",
        planner=_Planner(
            planning_result=SimpleNamespace(planning_state=PLANNING_STATE_COMPLETED, product_escalations=()),
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
    child_sync_gateway = _ChildSyncGateway(seed_data={"requires_input": True, "questions": []})
    with pytest.raises(RuntimeError, match="blocked but did not return clarification questions"):
        ParentPlanningFanoutService().plan_and_seed(
            parent_detail=SimpleNamespace(key="MAB-229"),
            product_brief={"objective": "Create child tickets"},
            project_key="MAB",
            planner=_Planner(
                planning_result=SimpleNamespace(planning_state="planning_needs_clarification", product_escalations=()),
                planning_package={"child_ticket_specs": []},
            ),
            child_sync_gateway=child_sync_gateway,
        )
    assert child_sync_gateway.seed_calls == 0


def test_parent_planning_fanout_service_returns_blocking_questions() -> None:
    child_sync_gateway = _ChildSyncGateway(seed_data={"requires_input": True, "questions": []})
    result = ParentPlanningFanoutService().plan_and_seed(
        parent_detail=SimpleNamespace(key="MAB-229"),
        product_brief={"objective": "Create child tickets"},
        project_key="MAB",
        planner=_Planner(
            planning_result=SimpleNamespace(
                planning_state="planning_needs_clarification",
                product_escalations=[
                    {
                        "question": "What audit retention window should v1 support?",
                        "why_it_matters": "The answer changes product commitments.",
                    }
                ],
            ),
            planning_package={"child_ticket_specs": []},
        ),
        child_sync_gateway=child_sync_gateway,
    )

    assert result.completed is False
    assert result.changed_children == []
    assert result.questions[0].question == "What audit retention window should v1 support?"
    assert child_sync_gateway.seed_calls == 0


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
