from __future__ import annotations

from dataclasses import dataclass
import json

from orchestrator.core.agent_tools import allowed_tools_for_stage, execute_agent_tool
from orchestrator.core.runtime_invocation import AgentInvocationContext, invoke_runtime_json_with_tools
from orchestrator.core.codex_runtime import CodexRuntimeError, build_codex_runtime
from orchestrator.core.prompt_templates import render_prompt
from orchestrator.core.runtime_payload_models import DecisionPlannerPayload
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
        payload = invoke_runtime_json_with_tools(
            runtime=runtime,
            context=AgentInvocationContext(
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

    parsed_payload = DecisionPlannerPayload.from_payload(payload, classification=classification)
    return DecisionPlannerResult(
        gate_status=parsed_payload.gate_status,
        reason=parsed_payload.reason,
        questions=tuple(
            DecisionPlannerQuestion(
                question_id=item.question_id,
                kind=item.kind,
                question=item.question,
                status=item.status,
                detail=item.detail,
            )
            for item in parsed_payload.questions
        ),
        question_states=tuple(
            DecisionPlannerQuestion(
                question_id=item.question_id,
                kind=item.kind,
                question=item.question,
                status=item.status,
                detail=item.detail,
            )
            for item in parsed_payload.question_states
        ),
        resolved_items=parsed_payload.resolved_items,
        missing_items=parsed_payload.missing_items,
        captured_answer_summary=parsed_payload.captured_answer_summary,
    )
