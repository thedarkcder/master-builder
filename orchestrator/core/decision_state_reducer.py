from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from orchestrator.core.decision_types import (
    DecisionClassification,
    IngressDecision,
    PrecheckOutcome,
)


class DecisionStateTransition(str, Enum):
    OPEN_CYCLE_BLOCKED = "open_cycle_blocked"
    OPEN_CYCLE_CLEAR_AND_CLOSE = "open_cycle_clear_and_close"
    TERMINAL_GATE_CLOSED_CLEAR = "terminal_gate_closed_clear"
    REUSE_CLEAR_FINGERPRINT = "reuse_clear_fingerprint"
    EVALUATE_FRESH = "evaluate_fresh"


@dataclass(frozen=True)
class DecisionStateReducerInput:
    has_case: bool
    has_open_cycle: bool
    unresolved_question_count: int
    decision_gate_closed_permanently: bool
    case_classification: DecisionClassification
    case_issue_fingerprint: str
    current_issue_fingerprint: str


def reduce_decision_state_transition(*, input_state: DecisionStateReducerInput) -> DecisionStateTransition:
    if input_state.has_case and input_state.has_open_cycle:
        if input_state.unresolved_question_count > 0:
            return DecisionStateTransition.OPEN_CYCLE_BLOCKED
        return DecisionStateTransition.OPEN_CYCLE_CLEAR_AND_CLOSE

    if input_state.has_case and input_state.decision_gate_closed_permanently:
        return DecisionStateTransition.TERMINAL_GATE_CLOSED_CLEAR

    if (
        input_state.has_case
        and not input_state.has_open_cycle
        and input_state.case_classification is DecisionClassification.CLEAR
        and input_state.case_issue_fingerprint == input_state.current_issue_fingerprint
    ):
        return DecisionStateTransition.REUSE_CLEAR_FINGERPRINT

    return DecisionStateTransition.EVALUATE_FRESH


def case_state_for_decision(*, decision: IngressDecision) -> str:
    pre_check = decision.pre_check
    parsed_block_reason = PrecheckOutcome.parse(decision.block_reason)
    if parsed_block_reason is PrecheckOutcome.DECISION_GATE_REQUIRED:
        return "blocked_decision_gate"
    if parsed_block_reason in {PrecheckOutcome.GTD_REQUIRED, PrecheckOutcome.EXECUTION_BLOCKED}:
        return "blocked_gtd"
    if pre_check is not None and PrecheckOutcome.parse(getattr(pre_check, "outcome", None)) is PrecheckOutcome.READY_FOR_AGENT:
        return "ready_for_execution"
    return "clear"


def decision_reason(*, pre_check: object, classification: str) -> str | None:
    if pre_check is None:
        return None
    parsed_classification = DecisionClassification.parse(classification)
    if parsed_classification.includes_decision_gate:
        decision_gate = getattr(pre_check, "decision_gate", None)
        reason = str(getattr(decision_gate, "reason", "") or "").strip() if decision_gate is not None else ""
        if reason:
            return reason
    if parsed_classification.includes_gtd:
        missing = [
            str(item).strip()
            for item in getattr(pre_check, "gtd_missing_criteria", ())
            if str(item).strip()
        ]
        if missing:
            return "Missing GTD criteria: " + ", ".join(missing)
    return None


def is_question_driven_state(*, classification: DecisionClassification | str, block_reason: str | None) -> bool:
    parsed_classification = (
        classification if isinstance(classification, DecisionClassification) else DecisionClassification.parse(classification)
    )
    parsed_block_reason = PrecheckOutcome.parse(block_reason)
    return parsed_classification.blocks_execution and parsed_block_reason in {
        PrecheckOutcome.DECISION_GATE_REQUIRED,
        PrecheckOutcome.GTD_REQUIRED,
    }
