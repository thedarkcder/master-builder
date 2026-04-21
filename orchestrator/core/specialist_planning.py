from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
import json
from typing import Any

from sqlalchemy.orm import Session

from orchestrator.core.clarification_questions import ClarificationQuestion, ClarificationQuestionSet
from orchestrator.core.runtime_invocation import AgentInvocationContext
from orchestrator.core.codex_runtime import CodexRuntimeError
from orchestrator.core.prompt_templates import render_prompt
from orchestrator.core.runtime_stage_session import RuntimeStageSession

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
    workflow_id: str | None = None
    operation_id: str | None = None
    attempt_id: str | None = None
    attempt: int | None = None


@dataclass(frozen=True)
class SpecialistPlanningStageResult:
    planning_state: str
    persona_id: str
    role_label: str
    blocked: bool
    findings: tuple[str, ...]
    recommendations: tuple[str, ...]
    required_tasks: tuple[str, ...]
    child_ticket_specs: tuple[dict[str, object], ...]
    open_behavior_questions: tuple[ClarificationQuestion, ...]
    acceptance_impacts: tuple[str, ...]
    mermaid_diagram: str | None = None

    def to_payload(self) -> dict[str, object]:
        payload = {
            "planning_state": self.planning_state,
            "persona_id": self.persona_id,
            "role_label": self.role_label,
            "blocked": self.blocked,
            "findings": list(self.findings),
            "recommendations": list(self.recommendations),
            "required_tasks": list(self.required_tasks),
            "child_ticket_specs": [dict(spec) for spec in self.child_ticket_specs],
            "open_behavior_questions": [question.to_payload() for question in self.open_behavior_questions],
            "acceptance_impacts": list(self.acceptance_impacts),
        }
        if isinstance(self.mermaid_diagram, str) and self.mermaid_diagram.strip():
            payload["mermaid_diagram"] = self.mermaid_diagram.strip()
        return payload


@dataclass(frozen=True)
class SpecialistPlanningResult:
    planning_state: str
    stages: tuple[SpecialistPlanningStageResult, ...]
    findings: tuple[str, ...]
    recommendations: tuple[str, ...]
    required_tasks: tuple[str, ...]
    open_behavior_questions: tuple[ClarificationQuestion, ...]
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
            "open_behavior_questions": [question.to_payload() for question in self.open_behavior_questions],
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


def _question_list(value: object) -> tuple[ClarificationQuestion, ...]:
    if not isinstance(value, list):
        return ()
    return ClarificationQuestionSet.from_values(value).questions


def _child_ticket_specs(value: object) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list):
        return ()
    specs: list[dict[str, object]] = []
    for raw_item in value:
        if not isinstance(raw_item, dict):
            continue
        summary = " ".join(str(raw_item.get("summary") or "").split())
        capability = " ".join(str(raw_item.get("capability") or "").split())
        delivery = " ".join(str(raw_item.get("delivery") or "").split())
        expected_outcome = " ".join(str(raw_item.get("expected_outcome") or "").split())
        if not summary or not delivery:
            continue
        acceptance_criteria = _string_list(raw_item.get("acceptance_criteria"))
        how_to_test = _string_list(raw_item.get("how_to_test"))
        done_means = _string_list(raw_item.get("done_means"))
        dependencies = _string_list(raw_item.get("dependencies"))
        risks = _string_list(raw_item.get("risks"))
        labels = _string_list(raw_item.get("labels"))
        specs.append(
            {
                "summary": summary,
                "capability": capability,
                "delivery": delivery,
                "expected_outcome": expected_outcome,
                "acceptance_criteria": list(acceptance_criteria),
                "how_to_test": list(how_to_test),
                "done_means": list(done_means),
                "dependencies": list(dependencies),
                "risks": list(risks),
                "labels": list(labels),
            }
        )
    return tuple(specs)


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


def _merge_unique_questions(
    *sequences: Iterable[ClarificationQuestion],
) -> tuple[ClarificationQuestion, ...]:
    return ClarificationQuestionSet.from_values(
        question for sequence in sequences for question in sequence
    ).questions


def _json_dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _planning_stage_user_context(
    *,
    request: SpecialistPlanningRequest,
    stage: _PlanningStageDefinition,
    stage_session: RuntimeStageSession,
) -> dict[str, object]:
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
        **stage_session.tooling.governed_native_prompt_context(),
    }


def _run_stage(
    *,
    session: Session | None,
    settings: Any | None,
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
    invocation_context = AgentInvocationContext(
        channel="system",
        tenant_id=request.tenant_id,
        project_id=request.project_id,
        command="pm",
        stage=stage.planning_state,
        working_dir=request.working_dir,
        workflow_id=request.workflow_id,
        operation_id=request.operation_id,
        attempt_id=request.attempt_id,
        issue_key=request.parent_issue_key,
        attempt=request.attempt,
        reasoning_effort=stage.reasoning_effort,
    )
    stage_session = RuntimeStageSession.create(
        runtime=selected_runtime,
        context=invocation_context,
        policy_stage=stage.planning_state,
        session=session,
        settings=settings,
        issue_key=request.parent_issue_key,
    )
    system_prompt = render_prompt(stage.system_prompt_template)
    user_prompt = render_prompt(
        stage.user_prompt_template,
        **_planning_stage_user_context(request=request, stage=stage, stage_session=stage_session),
    )
    payload = stage_session.invoke_json(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
    )
    if not isinstance(payload, dict):
        raise CodexRuntimeError(f"Codex did not return a {stage.planning_state} JSON object")

    findings = _string_list(payload.get("findings"))
    recommendations = _string_list(payload.get("recommendations"))
    required_tasks = _string_list(payload.get("required_tasks"))
    child_ticket_specs = _child_ticket_specs(payload.get("child_ticket_specs"))
    open_behavior_questions = _question_list(payload.get("open_behavior_questions"))
    acceptance_impacts = _string_list(payload.get("acceptance_impacts"))
    mermaid_diagram = None
    if isinstance(payload.get("mermaid_diagram"), str):
        normalized_diagram = payload.get("mermaid_diagram", "").strip()
        mermaid_diagram = normalized_diagram or None
    blocked = bool(open_behavior_questions)

    return SpecialistPlanningStageResult(
        planning_state=stage.planning_state,
        persona_id=stage.persona_id,
        role_label=stage.role_label,
        blocked=blocked,
        findings=findings,
        recommendations=recommendations,
        required_tasks=required_tasks,
        child_ticket_specs=child_ticket_specs,
        open_behavior_questions=open_behavior_questions,
        acceptance_impacts=acceptance_impacts,
        mermaid_diagram=mermaid_diagram,
    )


def planning_output_key(*, stage: SpecialistPlanningStageResult) -> str:
    if stage.persona_id == "architect":
        return "architecture"
    if stage.persona_id == "security":
        return "security"
    if stage.persona_id == "qa":
        return "testing"
    return stage.planning_state


def build_runtime_seed_planning_package(
    *,
    result: SpecialistPlanningResult,
) -> dict[str, object]:
    stage_payloads = {
        planning_output_key(stage=stage): stage.to_payload()
        for stage in result.stages
    }
    architect_payload = stage_payloads.get("architecture")
    if architect_payload is not None and not isinstance(architect_payload, dict):
        raise ValueError("Architect planning payload must be an object")
    child_specs_raw = architect_payload.get("child_ticket_specs", []) if isinstance(architect_payload, dict) else []
    if not isinstance(child_specs_raw, list):
        raise ValueError("Architect planning payload must provide child_ticket_specs as a list")
    child_issues = [dict(spec) for spec in child_specs_raw if isinstance(spec, dict)]
    architect_required_tasks = architect_payload.get("required_tasks", []) if isinstance(architect_payload, dict) else []
    if not isinstance(architect_required_tasks, list):
        raise ValueError("Architect planning payload must provide required_tasks as a list")
    if (
        result.planning_state == PLANNING_STATE_COMPLETED
        and isinstance(architect_payload, dict)
        and architect_required_tasks
        and not child_issues
    ):
        raise ValueError("Planning completed without executable engineering child ticket specs")
    payload: dict[str, object] = {
        "planning_state": result.planning_state,
        "specialist_outputs": stage_payloads,
        "child_issues": child_issues,
    }
    if result.architecture_summary:
        payload["architecture_summary"] = list(result.architecture_summary)
    if isinstance(result.architecture_diagram, str) and result.architecture_diagram.strip():
        payload["architecture_diagram"] = result.architecture_diagram.strip()
    return payload


def run_specialist_planning_fanout(
    *,
    session: Session | None = None,
    settings: Any | None = None,
    runtime: object,
    request: SpecialistPlanningRequest,
    runtime_for_selector: Callable[[str], object] | None = None,
) -> SpecialistPlanningResult:
    stage_results = tuple(
        _run_stage(
            session=session,
            settings=settings,
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
    open_behavior_questions = _merge_unique_questions(
        *(stage_result.open_behavior_questions for stage_result in stage_results)
    )
    planning_state = (
        PLANNING_STATE_BLOCKED if blocked_stage_states or open_behavior_questions else PLANNING_STATE_COMPLETED
    )
    block_reason = None
    if planning_state == PLANNING_STATE_BLOCKED:
        block_reason = "; ".join(
            f"{stage_result.planning_state}: {stage_result.open_behavior_questions[0].question}"
            for stage_result in stage_results
            if stage_result.blocked and stage_result.open_behavior_questions
        )
    architect_stage = next((stage for stage in stage_results if stage.persona_id == "architect"), None)
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
        findings=_merge_unique(*(stage_result.findings for stage_result in stage_results)),
        recommendations=_merge_unique(*(stage_result.recommendations for stage_result in stage_results)),
        required_tasks=_merge_unique(*(stage_result.required_tasks for stage_result in stage_results)),
        open_behavior_questions=open_behavior_questions,
        acceptance_impacts=_merge_unique(*(stage_result.acceptance_impacts for stage_result in stage_results)),
        blocked_stage_states=blocked_stage_states,
        block_reason=block_reason,
        architecture_summary=architecture_summary,
        architecture_diagram=architecture_diagram,
    )
