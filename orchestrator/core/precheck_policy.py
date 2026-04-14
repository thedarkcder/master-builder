from __future__ import annotations

from dataclasses import dataclass
import json

from orchestrator.core.codex_invocation import CodexInvocationContext, invoke_codex_json
from orchestrator.core.codex_runtime import CodexRuntimeError, build_codex_runtime
from orchestrator.core.config import get_settings
from orchestrator.core.decision_gate import DecisionGateResult
from orchestrator.core.gtd import GoodToDoValidationResult
from orchestrator.core.prompt_templates import render_prompt


@dataclass(frozen=True)
class PrecheckPolicyResult:
    decision_gate: DecisionGateResult
    gtd: GoodToDoValidationResult


def _string_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


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
        payload = invoke_codex_json(
            runtime=runtime,
            context=CodexInvocationContext(
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

    decision_gate_reason = str(payload.get("reason") or "").strip()
    decision_gate_recommendation = str(payload.get("recommendation") or "").strip()
    if not decision_gate_reason:
        raise RuntimeError("Codex precheck policy evaluation returned empty decision_gate reason")
    if not decision_gate_recommendation:
        raise RuntimeError("Codex precheck policy evaluation returned empty decision_gate recommendation")

    decision_gate = DecisionGateResult(
        triggered=bool(payload.get("triggered")),
        reason=decision_gate_reason,
        missing_sections=_string_list(payload.get("missing_sections")),
        questions=_string_list(payload.get("questions")),
        recommendation=decision_gate_recommendation,
        tags=_string_list(payload.get("tags")),
    )

    gtd = GoodToDoValidationResult(
        valid=bool(payload.get("gtd_valid")),
        missing_criteria=_string_list(payload.get("gtd_missing_criteria")),
        clarification_questions=_string_list(payload.get("gtd_clarification_questions")),
    )
    if not gtd.valid and not gtd.clarification_questions:
        raise RuntimeError(
            "Codex precheck policy evaluation returned invalid GTD result without clarification questions"
        )

    return PrecheckPolicyResult(
        decision_gate=decision_gate,
        gtd=gtd,
    )
