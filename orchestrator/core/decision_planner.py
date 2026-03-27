from __future__ import annotations

from dataclasses import dataclass
import json

from orchestrator.core.agent_tools import allowed_tools_for_stage, execute_agent_tool
from orchestrator.core.codex_invocation import CodexInvocationContext, invoke_codex_json_with_tools
from orchestrator.core.codex_runtime import CodexRuntimeError, build_codex_runtime
from orchestrator.core.prompt_templates import render_prompt
from orchestrator.storage.models import DecisionCase, DecisionCycle, Project, Tenant


@dataclass(frozen=True)
class DecisionPlannerQuestion:
    question_id: str
    kind: str
    question: str
    status: str
    detail: str | None


@dataclass(frozen=True)
class DecisionPlannerResult:
    gate_status: str
    reason: str
    questions: tuple[DecisionPlannerQuestion, ...]
    question_states: tuple[DecisionPlannerQuestion, ...]
    resolved_items: tuple[str, ...]
    missing_items: tuple[str, ...]
    captured_answer_summary: str | None


def plan_decision_questions(
    *,
    session,
    settings,  # noqa: ANN001
    tenant: Tenant,
    project: Project,
    issue_key: str,
    source: str,
    classification: str,
    block_reason: str | None,
    case: DecisionCase | None,
    cycle: DecisionCycle | None,
) -> DecisionPlannerResult | None:
    if classification not in {"decision_gate", "gtd", "both"}:
        return None
    runtime = build_codex_runtime(session=session, settings=settings)
    allowed_tools = sorted(allowed_tools_for_stage("decision_planner"))
    try:
        payload = invoke_codex_json_with_tools(
            runtime=runtime,
            context=CodexInvocationContext(
                channel="system",
                tenant_id=tenant.tenant_id,
                project_id=project.project_id,
                command="policy",
                stage="decision_planner",
                working_dir=".",
                issue_key=issue_key,
                reasoning_effort="medium",
            ),
            system_prompt=render_prompt("policy/decision_planner_system.j2"),
            user_prompt=render_prompt(
                "policy/decision_planner_user.j2",
                tenant_id=tenant.tenant_id,
                project_id=project.project_id,
                issue_key=issue_key,
                source=source,
                classification=classification,
                block_reason=block_reason or "",
                cycle_id=cycle.cycle_id if cycle is not None else "",
                case_state=case.state if case is not None else "",
                current_questions_json=json.dumps(cycle.question_set_json if cycle is not None else []),
                current_cycle_metadata_json=json.dumps(cycle.metadata_json if cycle is not None else {}),
                allowed_tools_json=json.dumps(allowed_tools),
            ),
            allowed_tools=set(allowed_tools),
            execute_tool=lambda tool_name, tool_args: execute_agent_tool(
                session=session,
                settings=settings,
                tenant_id=tenant.tenant_id,
                project_id=project.project_id,
                run_id=None,
                issue_key=issue_key,
                stage="decision_planner",
                tool_name=tool_name,
                tool_args=tool_args,
            ),
        )
    except CodexRuntimeError as exc:
        raise RuntimeError(f"Decision planner failed: {exc}") from exc

    gate_status = str(payload.get("gate_status") or "").strip().lower()
    if gate_status not in {"clear", "blocked_decision_gate", "blocked_gtd", "blocked_both"}:
        raise RuntimeError("Decision planner returned invalid gate_status")
    reason = str(payload.get("reason") or "").strip()
    if gate_status != "clear" and not reason:
        raise RuntimeError("Decision planner returned blocked state without reason")

    def _normalize_questions(raw_value: object) -> list[DecisionPlannerQuestion]:
        normalized_questions: list[DecisionPlannerQuestion] = []
        if not isinstance(raw_value, list):
            return normalized_questions
        for item in raw_value:
            if not isinstance(item, dict):
                continue
            question_id = str(item.get("question_id") or "").strip()
            question = str(item.get("question") or "").strip()
            if not question_id or not question:
                continue
            kind = str(item.get("kind") or "").strip().lower() or (
                "gtd" if classification == "gtd" else "decision_gate"
            )
            if kind not in {"decision_gate", "gtd"}:
                kind = "decision_gate"
            status = str(item.get("status") or "").strip().lower() or "open"
            if status not in {"open", "answered", "accepted"}:
                status = "open"
            normalized_questions.append(
                DecisionPlannerQuestion(
                    question_id=question_id,
                    kind=kind,
                    question=question,
                    status=status,
                    detail=str(item.get("detail") or "").strip() or None,
                )
            )
        return normalized_questions

    normalized_questions = _normalize_questions(payload.get("questions"))
    normalized_question_states = _normalize_questions(payload.get("question_states"))
    if not normalized_question_states:
        normalized_question_states = list(normalized_questions)

    captured_answer_summary = str(payload.get("captured_answer_summary") or "").strip() or None
    return DecisionPlannerResult(
        gate_status=gate_status,
        reason=reason,
        questions=tuple(normalized_questions),
        question_states=tuple(normalized_question_states),
        resolved_items=tuple(
            str(item).strip() for item in payload.get("resolved_items", []) if str(item).strip()
        ) if isinstance(payload.get("resolved_items"), list) else (),
        missing_items=tuple(
            str(item).strip() for item in payload.get("missing_items", []) if str(item).strip()
        ) if isinstance(payload.get("missing_items"), list) else (),
        captured_answer_summary=captured_answer_summary,
    )
