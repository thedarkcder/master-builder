from __future__ import annotations

from dataclasses import dataclass
import json

from orchestrator.core.runtime_invocation import AgentInvocationContext, invoke_runtime_json
from orchestrator.core.codex_runtime import CodexRuntimeError, build_codex_runtime
from orchestrator.core.config import get_settings
from orchestrator.core.decision_gate import DecisionGateResult
from orchestrator.core.gtd import GoodToDoValidationResult
from orchestrator.core.prompt_templates import render_prompt
from orchestrator.core.runtime_payload_models import PrecheckPolicyPayload


@dataclass(frozen=True)
class PrecheckPolicyResult:
    decision_gate: DecisionGateResult
    gtd: GoodToDoValidationResult

def evaluate_precheck_policy(
    *,
    issue_summary: str | None,
    issue_description: str | None,
    recorded_answers: list[dict[str, str]] | None = None,
    tenant_id: str | None = None,
    project_id: str | None = None,
    issue_key: str | None = None,
    run_id: str | None = None,
) -> PrecheckPolicyResult:
    normalized_issue_summary = (issue_summary or "").strip()
    normalized_issue_description = (issue_description or "").strip()
    settings = get_settings()
    runtime = build_codex_runtime(session=None, settings=settings)
    try:
        payload = invoke_runtime_json(
            runtime=runtime,
            context=AgentInvocationContext(
                channel="system",
                tenant_id=tenant_id,
                project_id=project_id,
                command="policy",
                stage="precheck",
                working_dir=".",
                issue_key=issue_key,
                run_id=run_id,
                reasoning_effort="low",
                issue_description_chars=len(normalized_issue_description),
            ),
            system_prompt=render_prompt("policy/precheck_system.j2"),
            user_prompt=render_prompt(
                "policy/precheck_user.j2",
                issue_summary=normalized_issue_summary,
                issue_description=normalized_issue_description,
                recorded_answers_json=json.dumps(recorded_answers or []),
            ),
        )
    except CodexRuntimeError as exc:
        raise RuntimeError(f"Codex precheck policy evaluation failed: {exc}") from exc

    parsed_payload = PrecheckPolicyPayload.from_payload(payload)

    decision_gate = DecisionGateResult(
        triggered=parsed_payload.decision_gate_triggered,
        reason=parsed_payload.decision_gate_reason,
        missing_sections=parsed_payload.decision_gate_missing_sections,
        questions=parsed_payload.decision_gate_questions,
        recommendation=parsed_payload.decision_gate_recommendation,
        tags=parsed_payload.decision_gate_tags,
    )

    gtd = GoodToDoValidationResult(
        valid=parsed_payload.gtd_valid,
        missing_criteria=parsed_payload.gtd_missing_criteria,
        clarification_questions=parsed_payload.gtd_clarification_questions,
    )

    return PrecheckPolicyResult(
        decision_gate=decision_gate,
        gtd=gtd,
    )
