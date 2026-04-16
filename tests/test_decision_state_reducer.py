from __future__ import annotations

import unittest

from orchestrator.core.decision_state_machine import (
    DecisionState,
    DecisionStateTransition,
    resolve_decision_state_transition,
)
from orchestrator.core.decision_types import DecisionClassification


class DecisionStateReducerTests(unittest.TestCase):
    def test_open_cycle_with_unresolved_questions_stays_blocked(self) -> None:
        transition = resolve_decision_state_transition(
            state=DecisionState(
                has_case=True,
                has_open_cycle=True,
                unresolved_question_count=2,
                decision_gate_closed_permanently=False,
                case_classification=DecisionClassification.DECISION_GATE,
                case_issue_fingerprint="f1",
                current_issue_fingerprint="f2",
            )
        )
        self.assertEqual(transition, DecisionStateTransition.OPEN_CYCLE_BLOCKED)

    def test_open_cycle_without_unresolved_questions_closes_cycle(self) -> None:
        transition = resolve_decision_state_transition(
            state=DecisionState(
                has_case=True,
                has_open_cycle=True,
                unresolved_question_count=0,
                decision_gate_closed_permanently=False,
                case_classification=DecisionClassification.DECISION_GATE,
                case_issue_fingerprint="f1",
                current_issue_fingerprint="f2",
            )
        )
        self.assertEqual(transition, DecisionStateTransition.OPEN_CYCLE_CLEAR_AND_CLOSE)

    def test_terminally_closed_gate_reuses_clear_decision(self) -> None:
        transition = resolve_decision_state_transition(
            state=DecisionState(
                has_case=True,
                has_open_cycle=False,
                unresolved_question_count=0,
                decision_gate_closed_permanently=True,
                case_classification=DecisionClassification.CLEAR,
                case_issue_fingerprint="f1",
                current_issue_fingerprint="f1",
            )
        )
        self.assertEqual(transition, DecisionStateTransition.TERMINAL_GATE_CLOSED_CLEAR)

    def test_matching_clear_fingerprint_reuses_snapshot(self) -> None:
        transition = resolve_decision_state_transition(
            state=DecisionState(
                has_case=True,
                has_open_cycle=False,
                unresolved_question_count=0,
                decision_gate_closed_permanently=False,
                case_classification=DecisionClassification.CLEAR,
                case_issue_fingerprint="f1",
                current_issue_fingerprint="f1",
            )
        )
        self.assertEqual(transition, DecisionStateTransition.REUSE_CLEAR_FINGERPRINT)

    def test_falls_back_to_fresh_evaluation(self) -> None:
        transition = resolve_decision_state_transition(
            state=DecisionState(
                has_case=False,
                has_open_cycle=False,
                unresolved_question_count=0,
                decision_gate_closed_permanently=False,
                case_classification=DecisionClassification.CLEAR,
                case_issue_fingerprint="",
                current_issue_fingerprint="f1",
            )
        )
        self.assertEqual(transition, DecisionStateTransition.EVALUATE_FRESH)


if __name__ == "__main__":
    unittest.main()
