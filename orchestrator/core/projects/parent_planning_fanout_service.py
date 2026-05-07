from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from orchestrator.core.clarification.questions import ClarificationQuestion, ClarificationQuestionSet
from orchestrator.core.planning.specialist import PLANNING_STATE_COMPLETED


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

    def to_payload(self) -> dict[str, Any]:
        return {
            "seed_data": self.seed_data,
            "updated_children": list(self.updated_children),
            "created_children": list(self.created_children),
            "changed_children": list(self.changed_children),
            "questions": [question.to_payload() for question in self.questions],
        }


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
    def seed_evaluation_from_payload(self, payload: object) -> ParentPlanningSeedEvaluation:
        if not isinstance(payload, dict):
            raise RuntimeError("Stored parent planning seed evaluation payload must be an object")
        seed_data = payload.get("seed_data")
        if not isinstance(seed_data, dict):
            raise RuntimeError("Stored parent planning seed evaluation is missing seed_data")
        return ParentPlanningSeedEvaluation(
            seed_data=seed_data,
            updated_children=[str(item) for item in payload.get("updated_children") or []],
            created_children=[str(item) for item in payload.get("created_children") or []],
            changed_children=[str(item) for item in payload.get("changed_children") or []],
            questions=ClarificationQuestionSet.from_values(list(payload.get("questions") or [])).questions,
        )

    def blocked_planning_result(
        self,
        *,
        planning_result,
        planning_package: dict[str, Any],
        questions: tuple[ClarificationQuestion, ...] = (),
    ) -> ParentPlanningFanoutResult:
        seed_evaluation = self.evaluate_seed_data(
            seed_data={
                "requires_input": True,
                "updated_children": [],
                "created_children": [],
                "changed_children": [],
                "questions": [question.to_payload() for question in questions],
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
            pm_questions: list[object] = []
            for request in getattr(planning_result, "pm_decision_requests", ()) or ():
                pm_questions.append(
                    ClarificationQuestion(
                        question=str(getattr(request, "question", "") or "").strip(),
                        why_it_matters=str(getattr(request, "why_it_matters", "") or "").strip(),
                        source_ref="pm_decision_request",
                    )
                )
            questions = ClarificationQuestionSet.from_values(pm_questions)
            if questions:
                return questions.questions
        return ClarificationQuestionSet.from_values(list(seed_data.get("questions", []) or [])).questions
