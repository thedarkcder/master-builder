from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from orchestrator.core.clarification_questions import ClarificationQuestion, ClarificationQuestionSet
from orchestrator.core.specialist_planning import PLANNING_STATE_COMPLETED


@dataclass(frozen=True)
class ParentPlanningSeedEvaluation:
    seed_data: dict[str, Any]
    updated_children: list[str]
    created_children: list[str]
    changed_children: list[str]
    questions: tuple[ClarificationQuestion, ...]

    @property
    def completed(self) -> bool:
        return not bool(self.seed_data.get("requires_input"))


@dataclass(frozen=True)
class ParentPlanningFanoutResult:
    planning_result: Any
    planning_package: dict[str, Any]
    seed_evaluation: ParentPlanningSeedEvaluation

    @property
    def seed_data(self) -> dict[str, Any]:
        return self.seed_evaluation.seed_data

    @property
    def updated_children(self) -> list[str]:
        return self.seed_evaluation.updated_children

    @property
    def created_children(self) -> list[str]:
        return self.seed_evaluation.created_children

    @property
    def changed_children(self) -> list[str]:
        return self.seed_evaluation.changed_children

    @property
    def questions(self) -> tuple[ClarificationQuestion, ...]:
        return self.seed_evaluation.questions

    @property
    def completed(self) -> bool:
        return self.planning_result.planning_state == PLANNING_STATE_COMPLETED and self.seed_evaluation.completed


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
        if planning_result.planning_state != PLANNING_STATE_COMPLETED:
            return self.blocked_planning_result(
                planning_result=planning_result,
                planning_package=planning_package,
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
        seed_evaluation = self.evaluate_seed_data(
            seed_data=seed_data,
            combine_child_updates_fn=child_sync_gateway.combined_child_updates,
            planning_result=planning_result,
        )
        result = ParentPlanningFanoutResult(
            planning_result=planning_result,
            planning_package=planning_package,
            seed_evaluation=seed_evaluation,
        )
        return result

    def blocked_planning_result(
        self,
        *,
        planning_result,
        planning_package: dict[str, Any],
    ) -> ParentPlanningFanoutResult:
        seed_evaluation = self.evaluate_seed_data(
            seed_data={
                "requires_input": True,
                "updated_children": [],
                "created_children": [],
                "changed_children": [],
                "questions": [],
            },
            combine_child_updates_fn=lambda *, seed_data: ([], [], []),
            planning_result=planning_result,
        )
        return ParentPlanningFanoutResult(
            planning_result=planning_result,
            planning_package=planning_package,
            seed_evaluation=seed_evaluation,
        )

    def evaluate_seed_data(
        self,
        *,
        seed_data: dict[str, Any],
        combine_child_updates_fn: Callable[..., tuple[list[str], list[str], list[str]]],
        planning_result=None,
    ) -> ParentPlanningSeedEvaluation:
        updated_children, created_children, changed_children = combine_child_updates_fn(seed_data=seed_data)
        questions = self._questions_for_result(planning_result=planning_result, seed_data=seed_data)
        evaluation = ParentPlanningSeedEvaluation(
            seed_data=seed_data,
            updated_children=updated_children,
            created_children=created_children,
            changed_children=changed_children,
            questions=questions,
        )
        if not evaluation.completed and not evaluation.questions:
            raise RuntimeError("Parent planning seed result is blocked but did not return clarification questions")
        return evaluation

    def _questions_for_result(
        self,
        *,
        planning_result,
        seed_data: dict[str, Any],
    ) -> tuple[ClarificationQuestion, ...]:
        if planning_result is not None:
            product_escalation_questions: list[object] = []
            for escalation in getattr(planning_result, "product_escalations", ()) or ():
                to_question = getattr(escalation, "to_clarification_question", None)
                product_escalation_questions.append(to_question() if callable(to_question) else escalation)
            questions = ClarificationQuestionSet.from_values(product_escalation_questions)
            if questions:
                return questions.questions
        return ClarificationQuestionSet.from_values(list(seed_data.get("questions", []) or [])).questions
