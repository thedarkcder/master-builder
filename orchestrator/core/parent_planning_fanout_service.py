from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from orchestrator.core.clarification_questions import ClarificationQuestion, ClarificationQuestionSet
from orchestrator.core.specialist_planning import PLANNING_STATE_COMPLETED


@dataclass(frozen=True)
class ParentPlanningFanoutResult:
    planning_result: Any
    planning_package: dict[str, Any]
    seed_data: dict[str, Any]
    updated_children: list[str]
    created_children: list[str]
    changed_children: list[str]
    questions: tuple[ClarificationQuestion, ...]

    @property
    def completed(self) -> bool:
        return self.planning_result.planning_state == PLANNING_STATE_COMPLETED and not bool(
            self.seed_data.get("requires_input")
        )


class ParentPlanningFanoutSeedError(RuntimeError):
    def __init__(
        self,
        *,
        error: Exception,
        planning_result,
        planning_package: dict[str, Any],
    ) -> None:
        super().__init__(str(error))
        self.error = error
        self.planning_result = planning_result
        self.planning_package = planning_package


class ParentPlanningFanoutService:
    def plan_and_seed(
        self,
        *,
        parent_detail,
        product_brief: dict[str, Any],
        project_key: str,
        planner,
        child_sync_gateway,
    ) -> ParentPlanningFanoutResult:
        planning_result, planning_package = planner.plan_backlog_parent(
            parent_detail=parent_detail,
            product_brief=product_brief,
            project_key=project_key,
        )
        try:
            seed_data = child_sync_gateway.seed_parent_backlog_children(
                parent_detail=parent_detail,
                project_key=project_key,
                planning_package=planning_package,
                planning_state=planning_result.planning_state,
            )
        except Exception as exc:  # noqa: BLE001
            raise ParentPlanningFanoutSeedError(
                error=exc,
                planning_result=planning_result,
                planning_package=planning_package,
            ) from exc
        updated_children, created_children, changed_children = child_sync_gateway.combined_child_updates(
            seed_data=seed_data
        )
        result = ParentPlanningFanoutResult(
            planning_result=planning_result,
            planning_package=planning_package,
            seed_data=seed_data,
            updated_children=updated_children,
            created_children=created_children,
            changed_children=changed_children,
            questions=self._questions_for_result(planning_result=planning_result, seed_data=seed_data),
        )
        if not result.completed and not result.questions:
            raise RuntimeError("Parent planning fanout is blocked but did not return clarification questions")
        return result

    def _questions_for_result(
        self,
        *,
        planning_result,
        seed_data: dict[str, Any],
    ) -> tuple[ClarificationQuestion, ...]:
        questions = ClarificationQuestionSet.from_values(
            getattr(planning_result, "open_behavior_questions", ()) or ()
        )
        if questions:
            return questions.questions
        return ClarificationQuestionSet.from_values(list(seed_data.get("questions", []) or [])).questions
