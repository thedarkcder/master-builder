from __future__ import annotations

import unittest
from unittest.mock import patch

from orchestrator.core.pr_ready import PrReadinessResult
from orchestrator.core.reviewer import ReviewAgentGate
from orchestrator.tools.github_app import PullRequestDetails, PullRequestFileChange, WorkflowCheckSuite


class _FakeGitHubClient:
    def __init__(
        self,
        checks: list[WorkflowCheckSuite],
        files: list[PullRequestFileChange] | None = None,
        review_body: str | None = None,
    ):
        self._checks = checks
        self._files = files or []
        self._review_body = review_body or (
            "Good:\n- implemented\n\n"
            "Risks:\n- low\n\n"
            "Must-fix:\n- none\n\n"
            "Tests:\n- pytest -q\n\n"
            "Questions:\n- none\n\n"
            "Follow-ups:\n- none\n"
        )

    def get_pull_request_details(self, *, repo_full_name: str, pr_number: int):  # noqa: ANN001
        return PullRequestDetails(
            number=pr_number,
            html_url=f"https://github.com/{repo_full_name}/pull/{pr_number}",
            head_sha="abc123",
            title=f"PR {pr_number}",
            state="open",
            head_ref="feature/test",
            base_ref="main",
            body=self._review_body,
        )

    def list_check_suites(self, *, repo_full_name: str, ref: str):  # noqa: ANN001
        return list(self._checks)

    def list_pull_request_files(self, *, repo_full_name: str, pr_number: int):  # noqa: ANN001
        return list(self._files)


class ReviewerGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._readiness_patch = patch(
            "orchestrator.core.reviewer.evaluate_pr_readiness",
            side_effect=_stub_readiness_evaluator,
        )
        self._readiness_patch.start()

    def tearDown(self) -> None:
        self._readiness_patch.stop()

    def _gate(self, client: _FakeGitHubClient) -> ReviewAgentGate:
        return ReviewAgentGate(
            client,
            tenant_id="route25",
            project_id="route25-default",
        )

    def test_reviewer_emits_ready_only_when_checks_green(self) -> None:
        gate = self._gate(
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
        )

        self.assertTrue(signal.ready)
        self.assertEqual(signal.state, "ready")
        self.assertIn("✅ PR Ready", signal.message)

    def test_reviewer_reports_pending_without_ready_signal(self) -> None:
        gate = self._gate(
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
        )

        self.assertFalse(signal.ready)
        self.assertEqual(signal.state, "pending_checks")
        self.assertEqual(signal.message, "PR opened, checks running: Security")

    def test_reviewer_reports_failures_without_ready_signal(self) -> None:
        gate = self._gate(
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
        )

        self.assertFalse(signal.ready)
        self.assertEqual(signal.state, "failing_checks")
        self.assertEqual(signal.message, "PR checks failing: CI")

    def test_reviewer_reports_policy_violations_as_must_fix(self) -> None:
        gate = self._gate(
            _FakeGitHubClient(
                checks=[
                    WorkflowCheckSuite(name="CI", status="completed", conclusion="success"),
                    WorkflowCheckSuite(name="Security", status="completed", conclusion="success"),
                ],
                files=[
                    PullRequestFileChange(
                        filename="admin-ui/src/components/TenantForm.tsx",
                        patch="+ await new Promise((resolve) => setTimeout(resolve, 500));",
                    )
                ],
            )
        )

        signal = gate.evaluate_pr(
            repo_full_name="example/repo",
            pr_number=13,
        )

        self.assertFalse(signal.ready)
        self.assertEqual(signal.state, "policy_violations")
        self.assertEqual(signal.policy_pack, "react")
        self.assertTrue(signal.must_fix_findings)

    def test_reviewer_reports_missing_review_sections_as_not_ready(self) -> None:
        gate = self._gate(
            _FakeGitHubClient(
                checks=[
                    WorkflowCheckSuite(name="CI", status="completed", conclusion="success"),
                    WorkflowCheckSuite(name="Security", status="completed", conclusion="success"),
                ],
                review_body="Good:\n- done\n",
            )
        )
        signal = gate.evaluate_pr(
            repo_full_name="example/repo",
            pr_number=14,
        )
        self.assertFalse(signal.ready)
        self.assertEqual(signal.state, "missing_review_sections")
        self.assertIn("missing required sections", signal.message)

    def test_reviewer_reports_missing_test_coverage_for_source_changes(self) -> None:
        gate = self._gate(
            _FakeGitHubClient(
                checks=[
                    WorkflowCheckSuite(name="CI", status="completed", conclusion="success"),
                    WorkflowCheckSuite(name="Security", status="completed", conclusion="success"),
                ],
                files=[PullRequestFileChange(filename="orchestrator/core/reviewer.py", patch="+ change")],
            )
        )
        signal = gate.evaluate_pr(
            repo_full_name="example/repo",
            pr_number=15,
        )
        self.assertFalse(signal.ready)
        self.assertEqual(signal.state, "missing_test_coverage")

    def test_reviewer_accepts_jest_test_js_coverage(self) -> None:
        gate = self._gate(
            _FakeGitHubClient(
                checks=[
                    WorkflowCheckSuite(name="CI", status="completed", conclusion="success"),
                    WorkflowCheckSuite(name="Security", status="completed", conclusion="success"),
                ],
                files=[
                    PullRequestFileChange(filename="orchestrator/core/reviewer.py", patch="+ change"),
                    PullRequestFileChange(filename="orchestrator/core/reviewer.test.js", patch="+ test"),
                ],
            )
        )
        signal = gate.evaluate_pr(
            repo_full_name="example/repo",
            pr_number=16,
        )
        self.assertNotEqual(signal.state, "missing_test_coverage")

    def test_reviewer_accepts_src_test_java_coverage(self) -> None:
        gate = self._gate(
            _FakeGitHubClient(
                checks=[
                    WorkflowCheckSuite(name="CI", status="completed", conclusion="success"),
                    WorkflowCheckSuite(name="Security", status="completed", conclusion="success"),
                ],
                files=[
                    PullRequestFileChange(filename="src/main/java/com/example/Service.java", patch="+ change"),
                    PullRequestFileChange(
                        filename="src/test/java/com/example/ServiceTest.java",
                        patch="+ test",
                    ),
                ],
            )
        )
        signal = gate.evaluate_pr(
            repo_full_name="example/repo",
            pr_number=17,
        )
        self.assertNotEqual(signal.state, "missing_test_coverage")


def _stub_readiness_evaluator(  # noqa: ANN001
    *,
    review_summary_markdown,
    required_workflows,
    workflow_checks,
    tenant_id=None,
    project_id=None,
):
    _ = tenant_id, project_id
    check_by_name = {check.name: check for check in workflow_checks}
    missing_workflows = tuple(name for name in required_workflows if name not in check_by_name)
    if missing_workflows:
        return PrReadinessResult(
            ready=False,
            state="missing_checks",
            reason="Missing required workflows",
            missing_workflows=missing_workflows,
            pending_workflows=(),
            failing_workflows=(),
            missing_review_sections=(),
        )

    pending_workflows = tuple(
        check.name
        for check in workflow_checks
        if check.name in required_workflows
        and (check.status != "completed" or not check.conclusion)
    )
    if pending_workflows:
        return PrReadinessResult(
            ready=False,
            state="pending_checks",
            reason="Required workflows pending",
            missing_workflows=(),
            pending_workflows=pending_workflows,
            failing_workflows=(),
            missing_review_sections=(),
        )

    failing_workflows = tuple(
        check.name
        for check in workflow_checks
        if check.name in required_workflows
        and check.status == "completed"
        and check.conclusion != "success"
    )
    if failing_workflows:
        return PrReadinessResult(
            ready=False,
            state="failing_checks",
            reason="Required workflows failing",
            missing_workflows=(),
            pending_workflows=(),
            failing_workflows=failing_workflows,
            missing_review_sections=(),
        )

    required_sections = ("Good:", "Risks:", "Must-fix:", "Tests:", "Questions:", "Follow-ups:")
    missing_review_sections = tuple(
        section.rstrip(":")
        for section in required_sections
        if section.lower() not in str(review_summary_markdown or "").lower()
    )
    if missing_review_sections:
        return PrReadinessResult(
            ready=False,
            state="missing_review_sections",
            reason="Missing required review sections",
            missing_workflows=(),
            pending_workflows=(),
            failing_workflows=(),
            missing_review_sections=missing_review_sections,
        )

    return PrReadinessResult(
        ready=True,
        state="ready",
        reason="All checks passed",
        missing_workflows=(),
        pending_workflows=(),
        failing_workflows=(),
        missing_review_sections=(),
    )
