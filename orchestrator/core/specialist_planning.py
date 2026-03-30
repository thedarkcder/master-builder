from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
import json
from typing import Any

from orchestrator.core.runtime_invocation import AgentInvocationContext, invoke_runtime_json
from orchestrator.core.codex_runtime import CodexRuntimeError
from orchestrator.core.prompt_templates import render_prompt

PLANNING_STATE_ENGINEERING = "engineering_planning"
PLANNING_STATE_SECURITY = "security_planning"
PLANNING_STATE_TEST = "test_planning"
PLANNING_STATE_BLOCKED = "planning_blocked"
PLANNING_STATE_COMPLETED = "planning_completed"


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


@dataclass(frozen=True)
class SpecialistPlanningStageResult:
    planning_state: str
    persona_id: str
    role_label: str
    blocked: bool
    findings: tuple[str, ...]
    recommendations: tuple[str, ...]
    required_tasks: tuple[str, ...]
    open_behavior_questions: tuple[str, ...]
    acceptance_impacts: tuple[str, ...]

    def to_payload(self) -> dict[str, object]:
        return {
            "planning_state": self.planning_state,
            "persona_id": self.persona_id,
            "role_label": self.role_label,
            "blocked": self.blocked,
            "findings": list(self.findings),
            "recommendations": list(self.recommendations),
            "required_tasks": list(self.required_tasks),
            "open_behavior_questions": list(self.open_behavior_questions),
            "acceptance_impacts": list(self.acceptance_impacts),
        }


@dataclass(frozen=True)
class SpecialistPlanningResult:
    planning_state: str
    stages: tuple[SpecialistPlanningStageResult, ...]
    findings: tuple[str, ...]
    recommendations: tuple[str, ...]
    required_tasks: tuple[str, ...]
    open_behavior_questions: tuple[str, ...]
    acceptance_impacts: tuple[str, ...]
    blocked_stage_states: tuple[str, ...]
    block_reason: str | None

    def to_payload(self) -> dict[str, object]:
        return {
            "planning_state": self.planning_state,
            "stages": [stage.to_payload() for stage in self.stages],
            "findings": list(self.findings),
            "recommendations": list(self.recommendations),
            "required_tasks": list(self.required_tasks),
            "open_behavior_questions": list(self.open_behavior_questions),
            "acceptance_impacts": list(self.acceptance_impacts),
            "blocked_stage_states": list(self.blocked_stage_states),
            "block_reason": self.block_reason,
        }


@dataclass(frozen=True)
class _PlanningStageDefinition:
    planning_state: str
    persona_id: str
    role_label: str
    selector: str
    system_prompt_template: str
    user_prompt_template: str
    reasoning_effort: str


_PLANNING_STAGES = (
    _PlanningStageDefinition(
        planning_state=PLANNING_STATE_ENGINEERING,
        persona_id="architect",
        role_label="Architect",
        selector="workflow.pm_planning_architect",
        system_prompt_template="workflow/pm_planning_architect_system.j2",
        user_prompt_template="workflow/pm_planning_architect_user.j2",
        reasoning_effort="high",
    ),
    _PlanningStageDefinition(
        planning_state=PLANNING_STATE_SECURITY,
        persona_id="security",
        role_label="Security",
        selector="workflow.pm_planning_security",
        system_prompt_template="workflow/pm_planning_security_system.j2",
        user_prompt_template="workflow/pm_planning_security_user.j2",
        reasoning_effort="medium",
    ),
    _PlanningStageDefinition(
        planning_state=PLANNING_STATE_TEST,
        persona_id="qa",
        role_label="QA",
        selector="workflow.pm_planning_tester",
        system_prompt_template="workflow/pm_planning_tester_system.j2",
        user_prompt_template="workflow/pm_planning_tester_user.j2",
        reasoning_effort="medium",
    ),
)


def _string_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    normalized: list[str] = []
    for item in value:
        text = " ".join(str(item).split())
        if text:
            normalized.append(text)
    return tuple(normalized)


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


def _json_dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _planning_stage_user_context(*, request: SpecialistPlanningRequest, stage: _PlanningStageDefinition) -> dict[str, object]:
    return {
        "parent_issue_key": request.parent_issue_key,
        "parent_summary": request.parent_summary,
        "parent_description": request.parent_description,
        "product_brief_json": _json_dump(request.product_brief),
        "project_keys_json": _json_dump(list(request.project_keys)),
        "related_issues_json": _json_dump(list(request.related_issues)),
        "status_counts_json": _json_dump(request.status_counts),
        "github_context_json": _json_dump(request.github_context),
        "conversation_history_json": _json_dump(list(request.conversation_history)),
        "stage_state": stage.planning_state,
        "persona_id": stage.persona_id,
        "role_label": stage.role_label,
    }


def _run_stage(
    *,
    runtime: object,
    runtime_for_selector: Callable[[str], object] | None,
    request: SpecialistPlanningRequest,
    stage: _PlanningStageDefinition,
) -> SpecialistPlanningStageResult:
    selected_runtime = runtime
    if callable(runtime_for_selector):
        resolved = runtime_for_selector(stage.selector)
        if resolved is not None:
            selected_runtime = resolved

    payload = invoke_runtime_json(
        runtime=selected_runtime,
        context=AgentInvocationContext(
            channel="system",
            tenant_id=request.tenant_id,
            project_id=request.project_id,
            command="pm",
            stage=stage.planning_state,
            working_dir=request.working_dir,
            issue_key=request.parent_issue_key,
            reasoning_effort=stage.reasoning_effort,
        ),
        system_prompt=render_prompt(stage.system_prompt_template),
        user_prompt=render_prompt(stage.user_prompt_template, **_planning_stage_user_context(request=request, stage=stage)),
    )
    if not isinstance(payload, dict):
        raise CodexRuntimeError(f"Codex did not return a {stage.planning_state} JSON object")

    findings = _string_list(payload.get("findings"))
    recommendations = _string_list(payload.get("recommendations"))
    required_tasks = _string_list(payload.get("required_tasks"))
    open_behavior_questions = _string_list(payload.get("open_behavior_questions"))
    acceptance_impacts = _string_list(payload.get("acceptance_impacts"))
    blocked = bool(open_behavior_questions)

    return SpecialistPlanningStageResult(
        planning_state=stage.planning_state,
        persona_id=stage.persona_id,
        role_label=stage.role_label,
        blocked=blocked,
        findings=findings,
        recommendations=recommendations,
        required_tasks=required_tasks,
        open_behavior_questions=open_behavior_questions,
        acceptance_impacts=acceptance_impacts,
    )


def run_specialist_planning_fanout(
    *,
    runtime: object,
    request: SpecialistPlanningRequest,
    runtime_for_selector: Callable[[str], object] | None = None,
) -> SpecialistPlanningResult:
    stage_results = tuple(
        _run_stage(
            runtime=runtime,
            runtime_for_selector=runtime_for_selector,
            request=request,
            stage=stage,
        )
        for stage in _PLANNING_STAGES
    )

    blocked_stage_states = tuple(
        stage_result.planning_state for stage_result in stage_results if stage_result.blocked
    )
    open_behavior_questions = _merge_unique(
        *(stage_result.open_behavior_questions for stage_result in stage_results)
    )
    planning_state = (
        PLANNING_STATE_BLOCKED if blocked_stage_states or open_behavior_questions else PLANNING_STATE_COMPLETED
    )
    block_reason = None
    if planning_state == PLANNING_STATE_BLOCKED:
        block_reason = "; ".join(
            f"{stage_result.planning_state}: {stage_result.open_behavior_questions[0]}"
            for stage_result in stage_results
            if stage_result.blocked and stage_result.open_behavior_questions
        )

    return SpecialistPlanningResult(
        planning_state=planning_state,
        stages=stage_results,
        findings=_merge_unique(*(stage_result.findings for stage_result in stage_results)),
        recommendations=_merge_unique(*(stage_result.recommendations for stage_result in stage_results)),
        required_tasks=_merge_unique(*(stage_result.required_tasks for stage_result in stage_results)),
        open_behavior_questions=open_behavior_questions,
        acceptance_impacts=_merge_unique(*(stage_result.acceptance_impacts for stage_result in stage_results)),
        blocked_stage_states=blocked_stage_states,
        block_reason=block_reason,
    )
