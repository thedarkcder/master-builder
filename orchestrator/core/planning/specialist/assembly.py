from __future__ import annotations

from orchestrator.core.planning.specialist.constants import PLANNING_STATE_COMPLETED
from orchestrator.core.planning.specialist.contract_parser import planning_output_key
from orchestrator.core.planning.specialist.models import (
    ArchitectStageOutput,
    RetryableSpecialistPlanningContractError,
    SpecialistPlanningResult,
)


class PlanningPackageAssembler:
    def build(self, *, result: SpecialistPlanningResult) -> dict[str, object]:
        stage_payloads = {
            planning_output_key(stage=stage): stage.to_payload()
            for stage in result.stages
        }
        architect_stage = next(
            (
                stage
                for stage in result.stages
                if isinstance(stage, ArchitectStageOutput)
            ),
            None,
        )
        child_issues = (
            [spec.to_payload() for spec in architect_stage.child_ticket_specs]
            if architect_stage
            else []
        )
        architect_required_tasks = (
            architect_stage.required_tasks if architect_stage else ()
        )
        if (
            result.planning_state == PLANNING_STATE_COMPLETED
            and architect_stage is not None
            and architect_required_tasks
            and not child_issues
        ):
            raise RetryableSpecialistPlanningContractError(
                "Codex returned planning_completed without executable engineering child ticket specs"
            )
        payload: dict[str, object] = {
            "planning_state": result.planning_state,
            "specialist_outputs": stage_payloads,
            "child_issues": child_issues,
            "technical_decisions": [
                decision.to_payload() for decision in result.technical_decisions
            ],
            "pm_decision_requests": [
                request.to_payload() for request in result.pm_decision_requests
            ],
        }
        if result.architecture_summary:
            payload["architecture_summary"] = list(result.architecture_summary)
        if (
            isinstance(result.architecture_diagram, str)
            and result.architecture_diagram.strip()
        ):
            payload["architecture_diagram"] = result.architecture_diagram.strip()
        return payload


def build_runtime_seed_planning_package(
    *,
    result: SpecialistPlanningResult,
) -> dict[str, object]:
    return PlanningPackageAssembler().build(result=result)
