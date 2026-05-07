from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from orchestrator.core.runtime.runtime import CodexRuntimeError
from orchestrator.core.planning.specialist.constants import (
    PLANNING_STATE_ENGINEERING,
    PLANNING_STATE_SECURITY,
    PLANNING_STATE_TEST,
)
from orchestrator.core.runtime.payload_models import (
    ArchitectStageOutput,
    ChildTicketSpec,
    PMDecisionRequest,
    PlanningStageOutput,
    SecurityStageOutput,
    TechnicalDecision,
    TestingStageOutput,
)


class RetryableSpecialistPlanningContractError(CodexRuntimeError):
    """Raised when model output violates the specialist-planning schema contract."""


@dataclass(frozen=True)
class SpecialistPlanningRequest:
    tenant_id: str
    project_id: str | None
    parent_issue_key: str
    parent_summary: str
    parent_description: str
    product_brief: dict[str, Any]
    project_keys: tuple[str, ...] = ()
    related_issues: tuple[dict[str, Any], ...] = ()
    status_counts: dict[str, int] = field(default_factory=dict)
    github_context: dict[str, Any] = field(default_factory=dict)
    conversation_history: tuple[dict[str, Any], ...] = ()
    working_dir: str = "."
    workflow_id: str | None = None
    operation_id: str | None = None
    attempt_id: str | None = None
    attempt: int | None = None
    work_unit_keys_by_stage: dict[str, str] = field(default_factory=dict)


SpecialistPlanningStageResult = ArchitectStageOutput | SecurityStageOutput | TestingStageOutput

__all__ = [
    "ArchitectStageOutput",
    "ChildTicketSpec",
    "PlanningStageOutput",
    "PMDecisionRequest",
    "RetryableSpecialistPlanningContractError",
    "SecurityStageOutput",
    "SpecialistPlanningRequest",
    "SpecialistPlanningResult",
    "SpecialistPlanningStageResult",
    "TechnicalDecision",
    "TestingStageOutput",
]


@dataclass(frozen=True)
class SpecialistPlanningResult:
    planning_state: str
    stages: tuple[SpecialistPlanningStageResult, ...]
    findings: tuple[str, ...]
    recommendations: tuple[str, ...]
    required_tasks: tuple[str, ...]
    technical_decisions: tuple[TechnicalDecision, ...]
    pm_decision_requests: tuple[PMDecisionRequest, ...]
    acceptance_impacts: tuple[str, ...]
    blocked_stage_states: tuple[str, ...]
    block_reason: str | None
    architecture_summary: tuple[str, ...] = ()
    architecture_diagram: str | None = None

    def to_payload(self) -> dict[str, object]:
        payload = {
            "planning_state": self.planning_state,
            "stages": [stage.to_payload() for stage in self.stages],
            "findings": list(self.findings),
            "recommendations": list(self.recommendations),
            "required_tasks": list(self.required_tasks),
            "technical_decisions": [decision.to_payload() for decision in self.technical_decisions],
            "pm_decision_requests": [request.to_payload() for request in self.pm_decision_requests],
            "acceptance_impacts": list(self.acceptance_impacts),
            "blocked_stage_states": list(self.blocked_stage_states),
            "block_reason": self.block_reason,
        }
        if self.architecture_summary:
            payload["architecture_summary"] = list(self.architecture_summary)
        if isinstance(self.architecture_diagram, str) and self.architecture_diagram.strip():
            payload["architecture_diagram"] = self.architecture_diagram.strip()
        return payload


@dataclass(frozen=True)
class PlanningStageDefinition:
    planning_state: str
    persona_id: str
    role_label: str
    selector: str
    system_prompt_template: str
    user_prompt_template: str
    reasoning_effort: str


PLANNING_STAGES = (
    PlanningStageDefinition(
        planning_state=PLANNING_STATE_ENGINEERING,
        persona_id="architect",
        role_label="Architect",
        selector="workflow.pm_planning_architect",
        system_prompt_template="workflow/pm_planning_architect_system.j2",
        user_prompt_template="workflow/pm_planning_architect_user.j2",
        reasoning_effort="high",
    ),
    PlanningStageDefinition(
        planning_state=PLANNING_STATE_SECURITY,
        persona_id="security",
        role_label="Security",
        selector="workflow.pm_planning_security",
        system_prompt_template="workflow/pm_planning_security_system.j2",
        user_prompt_template="workflow/pm_planning_security_user.j2",
        reasoning_effort="medium",
    ),
    PlanningStageDefinition(
        planning_state=PLANNING_STATE_TEST,
        persona_id="qa",
        role_label="QA",
        selector="workflow.pm_planning_tester",
        system_prompt_template="workflow/pm_planning_tester_system.j2",
        user_prompt_template="workflow/pm_planning_tester_user.j2",
        reasoning_effort="medium",
    ),
)
