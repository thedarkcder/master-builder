from __future__ import annotations

import re
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from orchestrator.api.discord.bug.gap_analysis import (
    extract_acceptance_criteria_from_description,
    gap_confidence,
    run_gap_analysis,
    tenant_repo_url,
)
from orchestrator.core.runs.service import RUN_STATUS_SUCCEEDED


class GapAnalysisTests(unittest.TestCase):
    def test_extract_acceptance_criteria_from_description(self) -> None:
        description = """
Objective

Acceptance Criteria:
- Must do one
- Should do two
Notes:
- Done
"""
        criteria = extract_acceptance_criteria_from_description(description)
        self.assertEqual(criteria, ["Must do one", "Should do two"])

        fallback = extract_acceptance_criteria_from_description("System should include support\nreturns summary")
        self.assertEqual(fallback, ["System should include support", "returns summary"])

    def test_gap_confidence(self) -> None:
        self.assertEqual(gap_confidence(has_acceptance=True, has_successful_run=True, has_pr=True), "high")
        self.assertEqual(gap_confidence(has_acceptance=True, has_successful_run=False, has_pr=True), "medium")
        self.assertEqual(gap_confidence(has_acceptance=False, has_successful_run=False, has_pr=False), "low")

    def test_tenant_repo_url(self) -> None:
        tenant = SimpleNamespace(repos_config={"github_repository": " https://github.com/acme/repo "})
        self.assertEqual(tenant_repo_url(tenant), "https://github.com/acme/repo")
        tenant_empty = SimpleNamespace(repos_config={})
        self.assertIsNone(tenant_repo_url(tenant_empty))

    def test_run_gap_analysis_validation_and_happy_path(self) -> None:
        session = MagicMock()
        tenant = SimpleNamespace(tenant_id="t1", repos_config={"github_repository": "https://github.com/acme/repo"})
        issue = SimpleNamespace(summary="Implement feature", description="Acceptance Criteria:\n- include tests")
        run = SimpleNamespace(run_id="run-1", status=RUN_STATUS_SUCCEEDED, pr_url="https://github.com/pr/1")
        session.execute.return_value.scalar_one_or_none.return_value = run

        with (
            patch("orchestrator.api.discord.bug.gap_analysis.fetch_jira_issue_detail_for_tenant", return_value=issue),
            patch("orchestrator.api.discord.bug.gap_analysis.resolve_tenant_jira_browse_base_url", return_value="https://jira.example.com"),
        ):
            message, data = run_gap_analysis(
                session=session,
                tenant=tenant,
                issue_key="mab-12",
                issue_key_pattern=re.compile(r"\b[A-Z][A-Z0-9_]+-\d+\b"),
            )

        self.assertIn("Gap analysis for", message)
        self.assertEqual(data["confidence"], "high")
        self.assertEqual(data["latest_run_status"], RUN_STATUS_SUCCEEDED)
        self.assertEqual(data["issue_key"], "MAB-12")

    def test_run_gap_analysis_reports_missing_run_and_pr(self) -> None:
        session = MagicMock()
        tenant = SimpleNamespace(tenant_id="t1", repos_config={})
        issue = SimpleNamespace(summary="Implement feature", description="")
        session.execute.return_value.scalar_one_or_none.return_value = None

        with (
            patch("orchestrator.api.discord.bug.gap_analysis.fetch_jira_issue_detail_for_tenant", return_value=issue),
            patch("orchestrator.api.discord.bug.gap_analysis.resolve_tenant_jira_browse_base_url", return_value=None),
        ):
            message, data = run_gap_analysis(
                session=session,
                tenant=tenant,
                issue_key="MAB-22",
                issue_key_pattern=re.compile(r"\b[A-Z][A-Z0-9_]+-\d+\b"),
            )

        self.assertIn("No run has been executed yet", "\n".join(data["gaps"]))
        self.assertIn("No PR is linked", "\n".join(data["gaps"]))
        self.assertEqual(data["confidence"], "low")
        self.assertNotIn("No obvious gaps", message)

    def test_run_gap_analysis_invalid_issue_key(self) -> None:
        with self.assertRaises(HTTPException):
            run_gap_analysis(
                session=MagicMock(),
                tenant=SimpleNamespace(tenant_id="t1", repos_config={}),
                issue_key="bad",
                issue_key_pattern=re.compile(r"\b[A-Z][A-Z0-9_]+-\d+\b"),
            )


if __name__ == "__main__":
    unittest.main()
