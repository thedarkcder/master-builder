from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from orchestrator.core.decision_types import DecisionClassification


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
