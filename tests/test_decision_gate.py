from __future__ import annotations

import unittest
from unittest.mock import patch

from orchestrator.core.decision.gate import (
    DecisionGateResult,
    evaluate_decision_gate,
    format_decision_gate_summary,
)


class DecisionGateTests(unittest.TestCase):
    def test_decision_gate_returns_runtime_payload(self) -> None:
        with patch(
            "orchestrator.core.decision.gate._evaluate_decision_gate_with_runtime",
            return_value=DecisionGateResult(
                triggered=True,
                reason="Missing GTD sections: Objective",
                missing_sections=("Objective",),
                questions=("What is the objective?",),
                recommendation="Decision required before build",
                tags=("[NEEDS-PM]",),
            ),
        ):
            result = evaluate_decision_gate(issue_summary="x", issue_description="y")

        self.assertTrue(result.triggered)
        self.assertIn("Objective", result.missing_sections)
        self.assertIn("Decision Gate required", format_decision_gate_summary(result))

    def test_decision_gate_clear_summary(self) -> None:
        with patch(
            "orchestrator.core.decision.gate._evaluate_decision_gate_with_runtime",
            return_value=DecisionGateResult(
                triggered=False,
                reason="Decision Gate not required",
                missing_sections=(),
                questions=(),
                recommendation="Proceed",
                tags=(),
            ),
        ):
            result = evaluate_decision_gate(issue_summary="x", issue_description="y")

        self.assertFalse(result.triggered)
        self.assertEqual(
            format_decision_gate_summary(result), "Decision Gate not required."
        )
