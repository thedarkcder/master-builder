from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from sqlalchemy.orm import Session

from orchestrator.core.planning.specialist.constants import (
    PLANNING_STATE_BLOCKED,
    PLANNING_STATE_COMPLETED,
)
from orchestrator.core.planning.specialist.models import (
    ArchitectStageOutput,
    PLANNING_STAGES,
    PMDecisionRequest,
    SpecialistPlanningRequest,
    SpecialistPlanningResult,
    SpecialistPlanningStageResult,
    TechnicalDecision,
)
from orchestrator.core.planning.specialist.stage_runner import (
    SpecialistPlanningStageRunner,
)


def _merge_unique(*sequences: Iterable[str]) -> tuple[str, ...]:
    merged: list[str] = []
    seen: set[str] = set()
    for sequence in sequences:
        for item in sequence:
            normalized = " ".join(str(item).split())
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            merged.append(normalized)
    return tuple(merged)


def _merge_unique_decisions(
    *sequences: Iterable[TechnicalDecision],
) -> tuple[TechnicalDecision, ...]:
    merged: list[TechnicalDecision] = []
    seen: set[str] = set()
    for sequence in sequences:
        for decision in sequence:
            key = decision.decision_id
            if key in seen:
                continue
            seen.add(key)
            merged.append(decision)
    return tuple(merged)


def _merge_unique_pm_decision_requests(
    *sequences: Iterable[PMDecisionRequest],
) -> tuple[PMDecisionRequest, ...]:
    return tuple(request for sequence in sequences for request in sequence)


class SpecialistPlanningFanoutService:
    def __init__(
        self, *, stage_runner: SpecialistPlanningStageRunner | None = None
    ) -> None:
        self._stage_runner = stage_runner or SpecialistPlanningStageRunner()

    def run(
        self,
        *,
        session: Session | None = None,
        settings: Any | None = None,
        runtime: object,
        request: SpecialistPlanningRequest,
        runtime_for_selector: Callable[[str], object] | None = None,
    ) -> SpecialistPlanningResult:
        stage_results = tuple(
            self._stage_runner.run_stage(
                session=session,
                settings=settings,
                runtime=runtime,
                runtime_for_selector=runtime_for_selector,
                request=request,
                stage=stage,
            )
            for stage in PLANNING_STAGES
        )
        return self._aggregate(stage_results=stage_results)

    def _aggregate(
        self,
        *,
        stage_results: tuple[SpecialistPlanningStageResult, ...],
    ) -> SpecialistPlanningResult:
        blocked_stage_states = tuple(
            stage_result.planning_state
            for stage_result in stage_results
            if stage_result.blocked
        )
        technical_decisions = _merge_unique_decisions(
            *(stage_result.technical_decisions for stage_result in stage_results)
        )
        pm_decision_requests = _merge_unique_pm_decision_requests(
            *(stage_result.pm_decision_requests for stage_result in stage_results)
        )
        planning_state = (
            PLANNING_STATE_BLOCKED
            if blocked_stage_states or pm_decision_requests
            else PLANNING_STATE_COMPLETED
        )
        block_reason = None
        if planning_state == PLANNING_STATE_BLOCKED:
            block_reason = "; ".join(
                f"{stage_result.planning_state}: {stage_result.pm_decision_requests[0].question}"
                for stage_result in stage_results
                if stage_result.blocked and stage_result.pm_decision_requests
            )
        architect_stage = next(
            (
                stage
                for stage in stage_results
                if isinstance(stage, ArchitectStageOutput)
            ),
            None,
        )
        architecture_summary = ()
        architecture_diagram = None
        if architect_stage is not None:
            architecture_summary = _merge_unique(
                architect_stage.findings,
                architect_stage.recommendations,
                architect_stage.acceptance_impacts,
            )
            architecture_diagram = architect_stage.mermaid_diagram

        return SpecialistPlanningResult(
            planning_state=planning_state,
            stages=stage_results,
            findings=_merge_unique(
                *(stage_result.findings for stage_result in stage_results)
            ),
            recommendations=_merge_unique(
                *(stage_result.recommendations for stage_result in stage_results)
            ),
            required_tasks=_merge_unique(
                *(stage_result.required_tasks for stage_result in stage_results)
            ),
            technical_decisions=technical_decisions,
            pm_decision_requests=pm_decision_requests,
            acceptance_impacts=_merge_unique(
                *(stage_result.acceptance_impacts for stage_result in stage_results)
            ),
            blocked_stage_states=blocked_stage_states,
            block_reason=block_reason,
            architecture_summary=architecture_summary,
            architecture_diagram=architecture_diagram,
        )


def run_specialist_planning_fanout(
    *,
    session: Session | None = None,
    settings: Any | None = None,
    runtime: object,
    request: SpecialistPlanningRequest,
    runtime_for_selector: Callable[[str], object] | None = None,
) -> SpecialistPlanningResult:
    return SpecialistPlanningFanoutService().run(
        session=session,
        settings=settings,
        runtime=runtime,
        request=request,
        runtime_for_selector=runtime_for_selector,
    )
