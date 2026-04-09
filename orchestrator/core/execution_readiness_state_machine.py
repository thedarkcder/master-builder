from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ReadinessState(str, Enum):
    READY = "ready"
    BLOCKED_DECISION = "blocked_decision"
    BLOCKED_READY_LABEL = "blocked_ready_label"
    POLICY_ERROR = "policy_error"


@dataclass(frozen=True)
class ReadinessDecision:
    state: ReadinessState
    reason_code: str | None = None


BLOCKING_OUTCOMES: frozenset[str] = frozenset(
    {
        "decision_gate_required",
        "gtd_required",
        "execution_blocked",
        "missing_ready_label",
    }
)


def blocking_reason_for_outcome(outcome: object) -> str | None:
    normalized = str(outcome or "").strip()
    if normalized in BLOCKING_OUTCOMES:
        return normalized
    return None


def resolve_readiness_decision(
    *,
    policy_error: str | None,
    block_reason: str | None,
    classification: str,
) -> ReadinessDecision:
    if str(policy_error or "").strip():
        return ReadinessDecision(
            state=ReadinessState.POLICY_ERROR,
            reason_code="policy_eval_failed",
        )

    normalized_block_reason = str(block_reason or "").strip()
    if normalized_block_reason == "missing_ready_label":
        return ReadinessDecision(
            state=ReadinessState.BLOCKED_READY_LABEL,
            reason_code="missing_ready_label",
        )

    if normalized_block_reason in {"decision_gate_required", "gtd_required", "execution_blocked"}:
        return ReadinessDecision(
            state=ReadinessState.BLOCKED_DECISION,
            reason_code=normalized_block_reason,
        )

    if classification in {"decision_gate", "gtd", "both"}:
        return ReadinessDecision(
            state=ReadinessState.BLOCKED_DECISION,
            reason_code=normalized_block_reason or "decision_gate_required",
        )

    return ReadinessDecision(state=ReadinessState.READY)


def resolve_precheck_outcome(
    *,
    decision_gate_triggered: bool,
    gtd_valid: bool,
    ready_label_present: bool,
    ready_label_required: bool,
) -> str:
    if decision_gate_triggered:
        return "decision_gate_required"
    if not gtd_valid:
        return "gtd_required"
    if ready_label_required and not ready_label_present:
        return "missing_ready_label"
    return "ready_for_agent"
