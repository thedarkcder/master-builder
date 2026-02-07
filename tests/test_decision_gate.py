from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from orchestrator.core.decision_gate import (
    evaluate_decision_gate,
    format_decision_gate_summary,
    reset_decision_gate_rules_cache,
)


class DecisionGateTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_decision_gate_rules_cache()

    def test_decision_gate_triggers_when_required_sections_missing(self) -> None:
        result = evaluate_decision_gate(
            issue_summary="Implement workflow",
            issue_description="Need to clarify rollout and constraints.",
        )

        self.assertTrue(result.triggered)
        self.assertIn("Missing GTD sections", result.reason)
        self.assertIn("Objective", result.missing_sections)
        self.assertEqual(len(result.questions), 5)
        self.assertIn("Decision required before build", format_decision_gate_summary(result))

    def test_decision_gate_passes_when_gtd_and_nfr_intent_present(self) -> None:
        description = """
        Objective: Improve orchestration reliability.
        Scope: in scope and out of scope are documented.
        Acceptance Criteria: explicit checks listed.
        How to test: run unit and integration checks.
        NFR intent: MVP first.
        """
        result = evaluate_decision_gate(
            issue_summary="MAB-3 implementation",
            issue_description=description,
        )

        self.assertFalse(result.triggered)
        self.assertEqual(result.reason, "Decision Gate not required")

    def test_decision_gate_rules_file_required(self) -> None:
        with self.assertRaises(FileNotFoundError):
            evaluate_decision_gate(
                issue_summary="MAB-3 implementation",
                issue_description="Objective: x\nScope: y\nAcceptance Criteria: z\nHow to test: t\nMVP",
                rules_path=Path("/tmp/not-a-real-decision-gate-rules.md"),
            )

    def test_decision_gate_uses_custom_rules_file(self) -> None:
        with TemporaryDirectory() as tmpdir:
            rules_path = Path(tmpdir) / "decision_gate.md"
            rules_path.write_text(
                "\n".join(
                    [
                        "# Decision Gate Rules",
                        "## Required sections",
                        "- Objective",
                        "## NFR markers",
                        "- mvp",
                        "## Ambiguity markers",
                        "- ???",
                        "## Resolution questions",
                        "- Q1?",
                        "## Tags",
                        "- [NEEDS-PM]",
                        "## Messages",
                        "- clear_reason: No gate",
                        "- blocked_recommendation: Stop",
                        "- clear_summary: clear",
                        "- blocked_title: blocked",
                        "- missing_sections_prefix: Missing",
                        "- ambiguity_prefix: Ambiguous",
                        "- options_line: options",
                    ]
                ),
                encoding="utf-8",
            )
            result = evaluate_decision_gate(
                issue_summary="Anything",
                issue_description="Objective: present. MVP.",
                rules_path=rules_path,
            )

        self.assertFalse(result.triggered)
        self.assertEqual(result.reason, "No gate")

    def test_decision_gate_falls_back_to_packaged_rules_when_local_file_missing(self) -> None:
        with TemporaryDirectory() as tmpdir:
            packaged_root = Path(tmpdir)
            (packaged_root / "decision_gate.md").write_text(
                "\n".join(
                    [
                        "# Decision Gate Rules",
                        "## Required sections",
                        "- Objective",
                        "- Scope",
                        "- Acceptance Criteria",
                        "- How to test",
                        "## NFR markers",
                        "- mvp",
                        "## Ambiguity markers",
                        "- tbd",
                        "## Resolution questions",
                        "- Q1?",
                        "## Tags",
                        "- [NEEDS-PM]",
                        "## Messages",
                        "- clear_reason: Fallback clear",
                        "- blocked_recommendation: Stop",
                        "- clear_summary: clear",
                        "- blocked_title: blocked",
                        "- missing_sections_prefix: Missing",
                        "- ambiguity_prefix: Ambiguous",
                        "- options_line: options",
                    ]
                ),
                encoding="utf-8",
            )

            with patch(
                "orchestrator.core.decision_gate.RULES_FILE_PATH",
                Path("/tmp/missing-decision-gate.md"),
            ), patch(
                "orchestrator.core.decision_gate.importlib.resources.files",
                return_value=packaged_root,
            ):
                result = evaluate_decision_gate(
                    issue_summary="MAB-3 implementation",
                    issue_description=(
                        "Objective: done.\n"
                        "Scope: done.\n"
                        "Acceptance Criteria: done.\n"
                        "How to test: done.\n"
                        "NFR intent: MVP."
                    ),
                )

        self.assertFalse(result.triggered)
        self.assertEqual(result.reason, "Fallback clear")
