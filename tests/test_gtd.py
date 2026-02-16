from __future__ import annotations

import unittest
from unittest.mock import patch

from orchestrator.core.gtd import GoodToDoValidationResult, validate_good_to_do


class GoodToDoValidationTests(unittest.TestCase):
    def test_valid_gtd_payload_passes(self) -> None:
        with patch(
            "orchestrator.core.gtd._validate_good_to_do_with_codex",
            return_value=GoodToDoValidationResult(valid=True, missing_criteria=(), clarification_questions=()),
        ):
            result = validate_good_to_do(
                issue_summary="Objective: improve webhook dispatch reliability",
                issue_description="Scope and acceptance criteria defined.",
            )

        self.assertTrue(result.valid)
        self.assertEqual(result.missing_criteria, ())
        self.assertEqual(result.clarification_questions, ())

    def test_missing_gtd_signals_return_questions(self) -> None:
        with patch(
            "orchestrator.core.gtd._validate_good_to_do_with_codex",
            return_value=GoodToDoValidationResult(
                valid=False,
                missing_criteria=("objective", "scope"),
                clarification_questions=("What is the objective?",),
            ),
        ):
            result = validate_good_to_do(
                issue_summary="Fix bug",
                issue_description="Please implement soon.",
            )

        self.assertFalse(result.valid)
        self.assertIn("objective", result.missing_criteria)
        self.assertIn("scope", result.missing_criteria)
        self.assertGreaterEqual(len(result.clarification_questions), 1)
