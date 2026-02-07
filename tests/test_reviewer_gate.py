from __future__ import annotations

import unittest

from orchestrator.core.reviewer import ReviewAgentGate
from orchestrator.tools.github_app import PullRequestDetails, WorkflowCheckSuite


class _FakeGitHubClient:
    def __init__(self, checks: list[WorkflowCheckSuite]):
        self._checks = checks

    def get_pull_request_details(self, *, repo_full_name: str, pr_number: int):  # noqa: ANN001
        return PullRequestDetails(
            number=pr_number,
            html_url=f"https://github.com/{repo_full_name}/pull/{pr_number}",
            head_sha="abc123",
        )

    def list_check_suites(self, *, repo_full_name: str, ref: str):  # noqa: ANN001
        return list(self._checks)


class ReviewerGateTests(unittest.TestCase):
    def test_reviewer_emits_ready_only_when_checks_green(self) -> None:
        gate = ReviewAgentGate(
            _FakeGitHubClient(
                checks=[
                    WorkflowCheckSuite(name="CI", status="completed", conclusion="success"),
                    WorkflowCheckSuite(name="Security", status="completed", conclusion="success"),
                ]
            )
        )
        signal = gate.evaluate_pr(
            repo_full_name="example/repo",
            pr_number=10,
            review_summary_present=True,
        )

        self.assertTrue(signal.ready)
        self.assertEqual(signal.state, "ready")
        self.assertIn("✅ PR Ready", signal.message)

    def test_reviewer_reports_pending_without_ready_signal(self) -> None:
        gate = ReviewAgentGate(
            _FakeGitHubClient(
                checks=[
                    WorkflowCheckSuite(name="CI", status="completed", conclusion="success"),
                    WorkflowCheckSuite(name="Security", status="queued", conclusion=None),
                ]
            )
        )
        signal = gate.evaluate_pr(
            repo_full_name="example/repo",
            pr_number=11,
            review_summary_present=True,
        )

        self.assertFalse(signal.ready)
        self.assertEqual(signal.state, "pending_checks")
        self.assertEqual(signal.message, "PR opened, checks running: Security")

    def test_reviewer_reports_failures_without_ready_signal(self) -> None:
        gate = ReviewAgentGate(
            _FakeGitHubClient(
                checks=[
                    WorkflowCheckSuite(name="CI", status="completed", conclusion="failure"),
                    WorkflowCheckSuite(name="Security", status="completed", conclusion="success"),
                ]
            )
        )
        signal = gate.evaluate_pr(
            repo_full_name="example/repo",
            pr_number=12,
            review_summary_present=True,
        )

        self.assertFalse(signal.ready)
        self.assertEqual(signal.state, "failing_checks")
        self.assertEqual(signal.message, "PR checks failing: CI")
