from __future__ import annotations

import json
from typing import Any

from orchestrator.core.runtime_invocation import AgentInvocationContext, invoke_runtime_json
from orchestrator.core.codex_runtime import CodexRuntime, CodexRuntimeError
from orchestrator.core.prompt_templates import render_prompt


def precheck_classification(pre_check: object) -> str:
    decision_gate = bool(getattr(pre_check, "decision_gate_triggered", False))
    gtd_valid_raw = getattr(pre_check, "gtd_valid", None)
    if isinstance(gtd_valid_raw, bool):
        gtd_missing = not gtd_valid_raw
    else:
        gtd_missing = bool(getattr(pre_check, "gtd_missing_criteria", ()) or getattr(pre_check, "gtd_clarification_questions", ()))
    if decision_gate and gtd_missing:
        return "both"
    if decision_gate:
        return "decision_gate"
    if gtd_missing:
        return "gtd"
    return "clear"


def _normalize_slot_name(raw_value: str) -> str:
    normalized = str(raw_value or "").strip().lower()
    normalized = normalized.replace("-", " ").replace("_", " ")
    canonical = {
        "objective": "objective",
        "scope": "scope",
        "acceptance criteria": "acceptance_criteria",
        "acceptance": "acceptance_criteria",
        "how to test": "how_to_test",
        "test plan": "how_to_test",
        "nfr intent": "nfr_intent",
        "nfr": "nfr_intent",
        "mvp vs scale-ready": "nfr_intent",
        "reliability/security constraints": "reliability_security_constraints",
        "reliability constraints": "reliability_security_constraints",
        "security constraints": "reliability_security_constraints",
        "out of scope": "out_of_scope",
        "rollout constraints": "rollout_constraints",
        "migration constraints": "rollout_constraints",
        "decision owner": "decision_owner",
        "dependencies / risks": "dependencies_and_risks",
        "dependencies and risks": "dependencies_and_risks",
        "risks": "dependencies_and_risks",
    }
    if normalized in canonical:
        return canonical[normalized]
    for key, value in canonical.items():
        if key in normalized:
            return value
    return normalized.replace(" ", "_")


def precheck_missing_slots(pre_check: object) -> list[str]:
    slots: list[str] = []
    decision_gate = getattr(pre_check, "decision_gate", None)
    if decision_gate is not None:
        missing_sections = getattr(decision_gate, "missing_sections", ())
        if isinstance(missing_sections, (list, tuple)):
            for item in missing_sections:
                normalized = _normalize_slot_name(str(item))
                if normalized and normalized not in slots:
                    slots.append(normalized)
    gtd_missing = getattr(pre_check, "gtd_missing_criteria", ())
    if isinstance(gtd_missing, (list, tuple)):
        for item in gtd_missing:
            normalized = _normalize_slot_name(str(item))
            if normalized and normalized not in slots:
                slots.append(normalized)
    return slots


def build_precheck_message(
    *,
    runtime: CodexRuntime,
    invocation_context: AgentInvocationContext,
    issue_key: str,
    classification: str,
    decision_gate_reason: str,
    decision_gate_questions: list[str],
    gtd_missing_criteria: list[str],
    gtd_questions: list[str],
    missing_slots: list[str],
) -> tuple[str, list[str]]:
    payload: dict[str, Any]
    try:
        payload = invoke_runtime_json(
            runtime=runtime,
            context=invocation_context,
            system_prompt=render_prompt("policy/precheck_message_system.j2"),
            user_prompt=render_prompt(
                "policy/precheck_message_user.j2",
                issue_key=issue_key,
                classification=classification,
                decision_gate_reason=decision_gate_reason,
                decision_gate_questions_json=json.dumps(decision_gate_questions),
                gtd_missing_criteria_json=json.dumps(gtd_missing_criteria),
                gtd_questions_json=json.dumps(gtd_questions),
                missing_slots_json=json.dumps(missing_slots),
            ),
        )
    except CodexRuntimeError:
        return _fallback_precheck_message(
            issue_key=issue_key,
            classification=classification,
            decision_gate_reason=decision_gate_reason,
            decision_gate_questions=decision_gate_questions,
            gtd_missing_criteria=gtd_missing_criteria,
            gtd_questions=gtd_questions,
        )

    message = str(payload.get("message") or "").strip()
    questions_raw = payload.get("questions")
    questions = (
        [str(item).strip() for item in questions_raw if str(item).strip()]
        if isinstance(questions_raw, list)
        else []
    )
    response_classification = str(payload.get("classification") or "").strip().lower()
    if not message or response_classification not in {"decision_gate", "gtd", "both", "clear"}:
        return _fallback_precheck_message(
            issue_key=issue_key,
            classification=classification,
            decision_gate_reason=decision_gate_reason,
            decision_gate_questions=decision_gate_questions,
            gtd_missing_criteria=gtd_missing_criteria,
            gtd_questions=gtd_questions,
        )
    return message, questions


def _fallback_precheck_message(
    *,
    issue_key: str,
    classification: str,
    decision_gate_reason: str,
    decision_gate_questions: list[str],
    gtd_missing_criteria: list[str],
    gtd_questions: list[str],
) -> tuple[str, list[str]]:
    lines = [f"Clarification is still needed for `{issue_key}`."]
    if classification in {"decision_gate", "both"}:
        lines.append(f"Decision Gate reason: {decision_gate_reason or 'clarification required'}")
    if classification in {"gtd", "both"} and gtd_missing_criteria:
        lines.append("Missing GTD criteria: " + ", ".join(gtd_missing_criteria))
    questions = [*decision_gate_questions, *gtd_questions]
    questions = [item for item in questions if item.strip()]
    if questions:
        lines.append("Please reply with:")
        lines.extend(f"- {question}" for question in questions[:6])
    return "\n".join(lines), questions[:6]
