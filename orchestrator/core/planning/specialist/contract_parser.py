from __future__ import annotations

from typing import Any

from orchestrator.core.runtime.runtime import CodexRuntimeError
from orchestrator.core.planning.specialist.models import (
    ArchitectStageOutput,
    PlanningStageDefinition,
    SecurityStageOutput,
    SpecialistPlanningStageResult,
    TestingStageOutput,
)


class StageContractParser:
    def parse(
        self,
        *,
        stage: PlanningStageDefinition,
        payload: dict[str, Any],
    ) -> SpecialistPlanningStageResult:
        if stage.persona_id == "architect":
            return ArchitectStageOutput.from_payload(
                planning_state=stage.planning_state,
                persona_id=stage.persona_id,
                role_label=stage.role_label,
                payload=payload,
            )
        if stage.persona_id == "security":
            return SecurityStageOutput.from_payload(
                planning_state=stage.planning_state,
                persona_id=stage.persona_id,
                role_label=stage.role_label,
                payload=payload,
            )
        if stage.persona_id == "qa":
            return TestingStageOutput.from_payload(
                planning_state=stage.planning_state,
                persona_id=stage.persona_id,
                role_label=stage.role_label,
                payload=payload,
            )
        raise CodexRuntimeError(
            f"Unsupported specialist planning persona '{stage.persona_id}'"
        )


def planning_output_key(*, stage: SpecialistPlanningStageResult) -> str:
    if isinstance(stage, ArchitectStageOutput):
        return "architecture"
    if isinstance(stage, SecurityStageOutput):
        return "security"
    if isinstance(stage, TestingStageOutput):
        return "testing"
    persona_id = getattr(stage, "persona_id", None)
    if persona_id == "architect":
        return "architecture"
    if persona_id == "security":
        return "security"
    if persona_id == "qa":
        return "testing"
    return stage.planning_state
