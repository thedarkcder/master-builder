from __future__ import annotations

import unittest

from orchestrator.core.decision.gate import DecisionGateResult
from orchestrator.core.decision.planner import DecisionPlannerQuestion, DecisionPlannerResult
from orchestrator.core.decision.state_machine import reduce_decision_planner_result
from orchestrator.core.decision.types import DecisionClassification, IngressDecision, PrecheckOutcome
from orchestrator.core.gtd import GoodToDoValidationResult
from orchestrator.core.precheck.pre_run_check import PreRunCheckResult


def _base_decision(*, outcome: PrecheckOutcome) -> IngressDecision:
    pre_check = PreRunCheckResult(
        outcome=outcome.value,
        ready_label="ready_for_agent",
        ready_label_present=False,
        required_worker_capability="",
        required_worker_label="",
        required_worker_label_present=True,
        decision_gate=DecisionGateResult(
            triggered=True,
            reason="Need clarification",
            missing_sections=("Dependencies",),
            questions=("What changed?",),
            recommendation="Clarify requirements.",
            tags=(),
        ),
        gtd=GoodToDoValidationResult(
            valid=False,
            missing_criteria=("Acceptance criteria",),
            clarification_questions=("What is the acceptance criteria?",),
        ),
    )
    return IngressDecision(
        source="jira_webhook",
        pre_check=pre_check,
        block_reason=outcome.value,
        guidance="",
        policy_error=None,
        label_actions=(),
    )


class DecisionReducerTests(unittest.TestCase):
    def test_reduce_planner_result_clear_resolves_to_ready_label_block(self) -> None:
        decision = _base_decision(outcome=PrecheckOutcome.DECISION_GATE_REQUIRED)
        planner_result = DecisionPlannerResult(
            gate_status="clear",
            reason="All questions answered.",
            questions=(),
            question_states=(),
            resolved_items=(),
            missing_items=(),
            captured_answer_summary=None,
        )

        reduced = reduce_decision_planner_result(decision=decision, planner_result=planner_result)

        self.assertIs(reduced.classification, DecisionClassification.CLEAR)
        self.assertIsNotNone(reduced.decision.pre_check)
        assert reduced.decision.pre_check is not None
        self.assertEqual(reduced.decision.pre_check.outcome, PrecheckOutcome.MISSING_READY_LABEL.value)
        self.assertEqual(reduced.decision.block_reason, PrecheckOutcome.MISSING_READY_LABEL.value)
        self.assertEqual(reduced.question_set, [])
        self.assertEqual(reduced.question_states, [])

    def test_reduce_planner_result_blocked_decision_gate_sets_block_and_questions(self) -> None:
        decision = _base_decision(outcome=PrecheckOutcome.DECISION_GATE_REQUIRED)
        planner_result = DecisionPlannerResult(
            gate_status="blocked_decision_gate",
            reason="Need PM clarification before execution.",
            questions=(
                DecisionPlannerQuestion(
                    question_id="q1",
                    kind="decision_gate",
                    question="What are the dependencies?",
                    status="open",
                    detail=None,
                ),
            ),
            question_states=(),
            resolved_items=(),
            missing_items=("Dependencies",),
            captured_answer_summary=None,
        )

        reduced = reduce_decision_planner_result(decision=decision, planner_result=planner_result)

        self.assertIs(reduced.classification, DecisionClassification.DECISION_GATE)
        self.assertIsNotNone(reduced.decision.pre_check)
        assert reduced.decision.pre_check is not None
        self.assertEqual(reduced.decision.pre_check.outcome, PrecheckOutcome.DECISION_GATE_REQUIRED.value)
        self.assertEqual(reduced.decision.block_reason, PrecheckOutcome.DECISION_GATE_REQUIRED.value)
        self.assertEqual(
            reduced.question_set,
            [
                {
                    "id": "q1",
                    "kind": "decision_gate",
                    "text": "What are the dependencies?",
                    "status": "open",
                    "detail": None,
                    "unresolved": True,
                }
            ],
        )

    def test_reduce_planner_result_marks_answered_state_unresolved_only_when_still_asked(self) -> None:
        decision = _base_decision(outcome=PrecheckOutcome.DECISION_GATE_REQUIRED)
        planner_result = DecisionPlannerResult(
            gate_status="blocked_decision_gate",
            reason="Need narrower clarification.",
            questions=(
                DecisionPlannerQuestion(
                    question_id="scope",
                    kind="decision_gate",
                    question="Which minimum HubSpot fields are required?",
                    status="open",
                    detail="The broad scope is known; the exact field contract is missing.",
                ),
            ),
            question_states=(
                DecisionPlannerQuestion(
                    question_id="scope",
                    kind="decision_gate",
                    question="What is in scope?",
                    status="answered",
                    detail="Scope is partially captured.",
                ),
                DecisionPlannerQuestion(
                    question_id="acceptance_criteria",
                    kind="decision_gate",
                    question="What are the acceptance criteria?",
                    status="answered",
                    detail="Acceptance criteria are already captured.",
                ),
            ),
            resolved_items=(),
            missing_items=("scope",),
            captured_answer_summary=None,
        )

        reduced = reduce_decision_planner_result(decision=decision, planner_result=planner_result)

        self.assertEqual(
            reduced.question_set,
            [
                {
                    "id": "scope",
                    "kind": "decision_gate",
                    "text": "Which minimum HubSpot fields are required?",
                    "status": "answered",
                    "detail": "The broad scope is known; the exact field contract is missing.",
                    "unresolved": True,
                },
                {
                    "id": "acceptance_criteria",
                    "kind": "decision_gate",
                    "text": "What are the acceptance criteria?",
                    "status": "answered",
                    "detail": "Acceptance criteria are already captured.",
                    "unresolved": False,
                },
            ],
        )


if __name__ == "__main__":
    unittest.main()
