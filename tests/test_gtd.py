from __future__ import annotations

import unittest

from orchestrator.core.gtd import validate_good_to_do


class GoodToDoValidationTests(unittest.TestCase):
    def test_valid_gtd_payload_passes(self) -> None:
        result = validate_good_to_do(
            issue_summary="Objective: improve webhook dispatch reliability",
            issue_description=(
                "Scope: in scope queueing; out of scope transport changes.\n"
                "Acceptance Criteria: retry works and idempotency preserved.\n"
                "Context: repo and component are documented.\n"
                "How to test: run integration tests.\n"
                "NFR intent: scale-ready.\n"
                "Risks: dependency on GitHub API limits."
            ),
        )

        self.assertTrue(result.valid)
        self.assertEqual(result.missing_criteria, ())
        self.assertEqual(result.clarification_questions, ())

    def test_missing_gtd_signals_return_questions(self) -> None:
        result = validate_good_to_do(
            issue_summary="Fix bug",
            issue_description="Please implement soon.",
        )

        self.assertFalse(result.valid)
        self.assertIn("objective", result.missing_criteria)
        self.assertIn("scope", result.missing_criteria)
        self.assertGreaterEqual(len(result.clarification_questions), 1)

