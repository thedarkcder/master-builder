from __future__ import annotations

from dataclasses import dataclass

from orchestrator.core.codex_invocation import CodexInvocationContext, invoke_codex_json
from orchestrator.core.codex_runtime import CodexRuntimeError, build_codex_runtime
from orchestrator.core.config import get_settings
from orchestrator.core.prompt_templates import render_prompt


@dataclass(frozen=True)
class DecisionGateResult:
    triggered: bool
    reason: str
    missing_sections: tuple[str, ...]
    questions: tuple[str, ...]
    recommendation: str
    tags: tuple[str, ...]

    def to_payload(self) -> dict[str, object]:
        return {
            "triggered": self.triggered,
            "reason": self.reason,
            "missing_sections": list(self.missing_sections),
            "questions": list(self.questions),
            "recommendation": self.recommendation,
            "tags": list(self.tags),
            "signal_summary": format_decision_gate_summary(self),
        }


@dataclass(frozen=True)
class DecisionGateRules:
    required_sections: tuple[str, ...]
    nfr_markers: tuple[str, ...]
    ambiguity_markers: tuple[str, ...]
    questions: tuple[str, ...]
    tags: tuple[str, ...]
    clear_reason: str
    blocked_recommendation: str
    clear_summary: str
    blocked_title: str
    missing_sections_prefix: str
    ambiguity_prefix: str
    options_line: str


def reset_decision_gate_rules_cache() -> None:
    return None


def load_decision_gate_rules(*, path: str | None = None) -> DecisionGateRules:
    _ = path
    return DecisionGateRules(
        required_sections=(),
        nfr_markers=(),
        ambiguity_markers=(),
        questions=(),
        tags=(),
        clear_reason="Decision Gate not required",
        blocked_recommendation="Decision required before build",
        clear_summary="Decision Gate not required.",
        blocked_title="Decision Gate required.",
        missing_sections_prefix="Missing GTD sections",
        ambiguity_prefix="Ambiguity markers found",
        options_line="Options: MVP quick delivery vs scale-ready design.",
    )


def _string_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _evaluate_decision_gate_with_codex(
    *,
    issue_summary: str,
    issue_description: str,
    tenant_id: str | None,
    project_id: str | None,
    issue_key: str | None,
    run_id: str | None,
) -> DecisionGateResult:
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
                stage="decision_gate",
                working_dir=".",
                issue_key=issue_key,
                run_id=run_id,
                reasoning_effort="low",
                issue_description_chars=len(issue_description or ""),
            ),
            system_prompt=render_prompt("policy/decision_gate_system.j2"),
            user_prompt=render_prompt(
                "policy/decision_gate_user.j2",
                issue_summary=issue_summary,
                issue_description=issue_description,
            ),
        )
    except CodexRuntimeError as exc:
        raise RuntimeError(f"Codex decision gate evaluation failed: {exc}") from exc

    triggered = bool(payload.get("triggered"))
    reason = str(payload.get("reason") or "").strip()
    missing_sections = _string_list(payload.get("missing_sections"))
    questions = _string_list(payload.get("questions"))
    recommendation = str(payload.get("recommendation") or "").strip()
    tags = _string_list(payload.get("tags"))

    if not reason:
        raise RuntimeError("Codex decision gate evaluation returned empty reason")
    if not recommendation:
        raise RuntimeError("Codex decision gate evaluation returned empty recommendation")

    return DecisionGateResult(
        triggered=triggered,
        reason=reason,
        missing_sections=missing_sections,
        questions=questions,
        recommendation=recommendation,
        tags=tags,
    )


def evaluate_decision_gate(
    *,
    issue_summary: str | None,
    issue_description: str | None,
    tenant_id: str | None = None,
    project_id: str | None = None,
    issue_key: str | None = None,
    run_id: str | None = None,
    rules_path: str | None = None,
) -> DecisionGateResult:
    _ = rules_path
    return _evaluate_decision_gate_with_codex(
        issue_summary=(issue_summary or "").strip(),
        issue_description=(issue_description or "").strip(),
        tenant_id=tenant_id,
        project_id=project_id,
        issue_key=issue_key,
        run_id=run_id,
    )


def format_decision_gate_summary(result: DecisionGateResult) -> str:
    if not result.triggered:
        return "Decision Gate not required."

    lines = [
        "Decision Gate required.",
        f"Reason: {result.reason}",
        "Options: MVP quick delivery vs scale-ready design.",
        f"Recommendation: {result.recommendation}",
    ]
    if result.questions:
        lines.append("Questions:")
        lines.extend(f"{idx}) {question}" for idx, question in enumerate(result.questions[:5], start=1))
    if result.tags:
        lines.append("Tags: " + " ".join(result.tags))
    return "\n".join(lines)
