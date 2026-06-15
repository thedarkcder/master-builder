from __future__ import annotations

import unittest
from unittest.mock import patch

from orchestrator.core.review.pr_ready import PrReadinessResult
from orchestrator.core.review.reviewer import ReviewAgentGate
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
            "orchestrator.core.review.reviewer.evaluate_pr_readiness",
            side_effect=_stub_readiness_evaluator,
        )
        self._readiness_patch.start()

    def tearDown(self) -> None:
        self._readiness_patch.stop()

    def _gate(self, client: _FakeGitHubClient) -> ReviewAgentGate:
        return ReviewAgentGate(
            client,
            tenant_id="example",
            project_id="example-default",
        )

    def _gate_with_demo_requirement(
        self,
        client: _FakeGitHubClient,
        *,
        required_targets: tuple[str, ...] = (),
        current_run_id: str = "run-1",
        expected_recordings: tuple[dict[str, str], ...] | None = None,
        use_recordings_resolver: bool = False,
    ) -> ReviewAgentGate:
        return ReviewAgentGate(
            client,
            require_demo_evidence=True,
            required_demo_capture_targets=required_targets,
            tenant_id="example",
            project_id="example-default",
            demo_artifact_public_base_url="https://cdn.example/qa-demos",
            demo_evidence_run_id_resolver=lambda _pr_url: current_run_id,
            demo_evidence_recordings_resolver=(lambda _run_id: expected_recordings)
            if expected_recordings is not None or use_recordings_resolver
            else None,
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

    def test_reviewer_blocks_housekeeping_files_mixed_into_source_pr(self) -> None:
        gate = self._gate(
            _FakeGitHubClient(
                checks=[
                    WorkflowCheckSuite(name="CI", status="completed", conclusion="success"),
                    WorkflowCheckSuite(name="Security", status="completed", conclusion="success"),
                ],
                files=[
                    PullRequestFileChange(filename="GirlPower/App/GirlPowerApp.swift", patch="+ change"),
                    PullRequestFileChange(filename="tasks/lessons.md", patch="+ lesson"),
                    PullRequestFileChange(filename="GirlPowerUITests/GirlPowerUITests.swift", patch="+ test"),
                ],
            )
        )

        signal = gate.evaluate_pr(
            repo_full_name="example/repo",
            pr_number=15,
        )

        self.assertFalse(signal.ready)
        self.assertEqual(signal.state, "out_of_scope_changes")
        self.assertIn("tasks/lessons.md", signal.message)

    def test_reviewer_blocks_ready_signal_when_demo_evidence_missing(self) -> None:
        gate = self._gate_with_demo_requirement(
            _FakeGitHubClient(
                checks=[
                    WorkflowCheckSuite(name="CI", status="completed", conclusion="success"),
                    WorkflowCheckSuite(name="Security", status="completed", conclusion="success"),
                ],
                files=[
                    PullRequestFileChange(filename="orchestrator/core/reviewer.py", patch="+ change"),
                    PullRequestFileChange(filename="tests/test_reviewer_gate.py", patch="+ test"),
                ],
            )
        )

        signal = gate.evaluate_pr(
            repo_full_name="example/repo",
            pr_number=15,
        )

        self.assertFalse(signal.ready)
        self.assertEqual(signal.state, "missing_demo_evidence")

    def test_reviewer_blocks_plain_demo_evidence_links(self) -> None:
        gate = self._gate_with_demo_requirement(
            _FakeGitHubClient(
                checks=[
                    WorkflowCheckSuite(name="CI", status="completed", conclusion="success"),
                    WorkflowCheckSuite(name="Security", status="completed", conclusion="success"),
                ],
                files=[
                    PullRequestFileChange(filename="orchestrator/core/reviewer.py", patch="+ change"),
                    PullRequestFileChange(filename="tests/test_reviewer_gate.py", patch="+ test"),
                ],
                review_body=(
                    "Good:\n- implemented\n\n"
                    "Risks:\n- low\n\n"
                    "Must-fix:\n- none\n\n"
                    "Tests:\n- pytest -q\n\n"
                    "Questions:\n- none\n\n"
                    "Follow-ups:\n- none\n\n"
                    "## Demo Evidence\n- Happy path: https://cdn.example/qa/happy.webm\n"
                ),
            )
        )

        signal = gate.evaluate_pr(
            repo_full_name="example/repo",
            pr_number=16,
        )

        self.assertFalse(signal.ready)
        self.assertEqual(signal.state, "missing_demo_evidence")

    def test_reviewer_blocks_structured_demo_evidence_without_required_target_and_count_markers(self) -> None:
        gate = self._gate_with_demo_requirement(
            _FakeGitHubClient(
                checks=[
                    WorkflowCheckSuite(name="CI", status="completed", conclusion="success"),
                    WorkflowCheckSuite(name="Security", status="completed", conclusion="success"),
                ],
                files=[
                    PullRequestFileChange(filename="orchestrator/core/reviewer.py", patch="+ change"),
                    PullRequestFileChange(filename="tests/test_reviewer_gate.py", patch="+ test"),
                ],
                review_body=(
                    "Good:\n- implemented\n\n"
                    "Risks:\n- low\n\n"
                    "Must-fix:\n- none\n\n"
                    "Tests:\n- pytest -q\n\n"
                    "Questions:\n- none\n\n"
                    "Follow-ups:\n- none\n\n"
                    "## Demo Evidence\n"
                    "<!-- master-builder:qa-demo-evidence v1 -->\n"
                    "- Happy path [target=browser; reference=https://preview.example; "
                    "object_key=tenant-1/project-1/run-1/qa-demo-1.webm]: "
                    "https://cdn.example/qa-demos/tenant-1/project-1/run-1/qa-demo-1.webm\n"
                ),
            )
        )

        signal = gate.evaluate_pr(
            repo_full_name="example/repo",
            pr_number=17,
        )

        self.assertFalse(signal.ready)
        self.assertEqual(signal.state, "missing_demo_evidence")

    def test_reviewer_blocks_structured_demo_evidence_when_url_does_not_match_object_key(self) -> None:
        gate = self._gate_with_demo_requirement(
            _FakeGitHubClient(
                checks=[
                    WorkflowCheckSuite(name="CI", status="completed", conclusion="success"),
                    WorkflowCheckSuite(name="Security", status="completed", conclusion="success"),
                ],
                files=[
                    PullRequestFileChange(filename="orchestrator/core/reviewer.py", patch="+ change"),
                    PullRequestFileChange(filename="tests/test_reviewer_gate.py", patch="+ test"),
                ],
                review_body=(
                    "Good:\n- implemented\n\n"
                    "Risks:\n- low\n\n"
                    "Must-fix:\n- none\n\n"
                    "Tests:\n- pytest -q\n\n"
                    "Questions:\n- none\n\n"
                    "Follow-ups:\n- none\n\n"
                    "## Demo Evidence\n"
                    "<!-- master-builder:qa-demo-evidence v1 -->\n"
                    "- Browser walkthrough [target=browser; reference=https://preview.example; "
                    "object_key=tenant-1/project-1/run-1/qa-demo-1.webm]: https://cdn.example/qa/browser.webm\n"
                ),
            )
        )

        signal = gate.evaluate_pr(
            repo_full_name="example/repo",
            pr_number=18,
        )

        self.assertFalse(signal.ready)
        self.assertEqual(signal.state, "missing_demo_evidence")

    def test_reviewer_blocks_structured_demo_evidence_from_unconfigured_artifact_host(self) -> None:
        gate = self._gate_with_demo_requirement(
            _FakeGitHubClient(
                checks=[
                    WorkflowCheckSuite(name="CI", status="completed", conclusion="success"),
                    WorkflowCheckSuite(name="Security", status="completed", conclusion="success"),
                ],
                files=[
                    PullRequestFileChange(filename="orchestrator/core/reviewer.py", patch="+ change"),
                    PullRequestFileChange(filename="tests/test_reviewer_gate.py", patch="+ test"),
                ],
                review_body=(
                    "Good:\n- implemented\n\n"
                    "Risks:\n- low\n\n"
                    "Must-fix:\n- none\n\n"
                    "Tests:\n- pytest -q\n\n"
                    "Questions:\n- none\n\n"
                    "Follow-ups:\n- none\n\n"
                    "## Demo Evidence\n"
                    "<!-- master-builder:qa-demo-evidence v1 -->\n"
                    "<!-- master-builder:qa-demo-required-targets browser -->\n"
                    "<!-- master-builder:qa-demo-required-counts browser=1 -->\n"
                    "- Browser walkthrough [target=browser; reference=https://preview.example; "
                    "object_key=example/example-default/run-1/qa-demo-1.webm; "
                    "sha256=0000000000000000000000000000000000000000000000000000000000000001; "
                    "release_commit_sha=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb; "
                    "release_context_sha256=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa]: "
                    "https://manual.example/qa-demos/example/example-default/run-1/qa-demo-1.webm\n"
                ),
            )
        )

        signal = gate.evaluate_pr(
            repo_full_name="example/repo",
            pr_number=18,
        )

        self.assertFalse(signal.ready)
        self.assertEqual(signal.state, "missing_demo_evidence")

    def test_reviewer_blocks_structured_demo_evidence_missing_required_target_marker(self) -> None:
        gate = self._gate_with_demo_requirement(
            _FakeGitHubClient(
                checks=[
                    WorkflowCheckSuite(name="CI", status="completed", conclusion="success"),
                    WorkflowCheckSuite(name="Security", status="completed", conclusion="success"),
                ],
                files=[
                    PullRequestFileChange(filename="orchestrator/core/reviewer.py", patch="+ change"),
                    PullRequestFileChange(filename="tests/test_reviewer_gate.py", patch="+ test"),
                ],
                review_body=(
                    "Good:\n- implemented\n\n"
                    "Risks:\n- low\n\n"
                    "Must-fix:\n- none\n\n"
                    "Tests:\n- pytest -q\n\n"
                    "Questions:\n- none\n\n"
                    "Follow-ups:\n- none\n\n"
                    "## Demo Evidence\n"
                    "<!-- master-builder:qa-demo-evidence v1 -->\n"
                    "<!-- master-builder:qa-demo-required-targets browser,ios,android -->\n"
                    "- Browser walkthrough [target=browser; reference=https://preview.example; "
                    "object_key=tenant-1/project-1/run-1/qa-demo-1.webm]: https://cdn.example/qa/browser.webm\n"
                ),
            )
        )

        signal = gate.evaluate_pr(
            repo_full_name="example/repo",
            pr_number=18,
        )

        self.assertFalse(signal.ready)
        self.assertEqual(signal.state, "missing_demo_evidence")

    def test_reviewer_blocks_structured_demo_evidence_missing_required_count_marker(self) -> None:
        gate = self._gate_with_demo_requirement(
            _FakeGitHubClient(
                checks=[
                    WorkflowCheckSuite(name="CI", status="completed", conclusion="success"),
                    WorkflowCheckSuite(name="Security", status="completed", conclusion="success"),
                ],
                files=[
                    PullRequestFileChange(filename="orchestrator/core/reviewer.py", patch="+ change"),
                    PullRequestFileChange(filename="tests/test_reviewer_gate.py", patch="+ test"),
                ],
                review_body=(
                    "Good:\n- implemented\n\n"
                    "Risks:\n- low\n\n"
                    "Must-fix:\n- none\n\n"
                    "Tests:\n- pytest -q\n\n"
                    "Questions:\n- none\n\n"
                    "Follow-ups:\n- none\n\n"
                    "## Demo Evidence\n"
                    "<!-- master-builder:qa-demo-evidence v1 -->\n"
                    "<!-- master-builder:qa-demo-required-targets browser,ios,android -->\n"
                    "- Browser walkthrough [target=browser; reference=https://preview.example; "
                    "object_key=tenant-1/project-1/run-1/qa-demo-1.webm]: "
                    "https://cdn.example/qa-demos/tenant-1/project-1/run-1/qa-demo-1.webm\n"
                    "- iOS walkthrough [target=ios; reference=ios-simulator://configured; "
                    "object_key=tenant-1/project-1/run-1/qa-demo-2.mp4]: "
                    "https://cdn.example/qa-demos/tenant-1/project-1/run-1/qa-demo-2.mp4\n"
                    "- Android walkthrough [target=android; reference=android-emulator://configured; "
                    "object_key=tenant-1/project-1/run-1/qa-demo-3.mp4]: "
                    "https://cdn.example/qa-demos/tenant-1/project-1/run-1/qa-demo-3.mp4\n"
                ),
            )
        )

        signal = gate.evaluate_pr(
            repo_full_name="example/repo",
            pr_number=19,
        )

        self.assertFalse(signal.ready)
        self.assertEqual(signal.state, "missing_demo_evidence")

    def test_reviewer_blocks_structured_demo_evidence_missing_required_recording_count_marker(self) -> None:
        gate = self._gate_with_demo_requirement(
            _FakeGitHubClient(
                checks=[
                    WorkflowCheckSuite(name="CI", status="completed", conclusion="success"),
                    WorkflowCheckSuite(name="Security", status="completed", conclusion="success"),
                ],
                files=[
                    PullRequestFileChange(filename="orchestrator/core/reviewer.py", patch="+ change"),
                    PullRequestFileChange(filename="tests/test_reviewer_gate.py", patch="+ test"),
                ],
                review_body=(
                    "Good:\n- implemented\n\n"
                    "Risks:\n- low\n\n"
                    "Must-fix:\n- none\n\n"
                    "Tests:\n- pytest -q\n\n"
                    "Questions:\n- none\n\n"
                    "Follow-ups:\n- none\n\n"
                    "## Demo Evidence\n"
                    "<!-- master-builder:qa-demo-evidence v1 -->\n"
                    "<!-- master-builder:qa-demo-required-targets browser -->\n"
                    "<!-- master-builder:qa-demo-required-counts browser=2 -->\n"
                    "- Browser walkthrough [target=browser; reference=https://preview.example; "
                    "object_key=tenant-1/project-1/run-1/qa-demo-1.webm]: https://cdn.example/qa/browser.webm\n"
                ),
            )
        )

        signal = gate.evaluate_pr(
            repo_full_name="example/repo",
            pr_number=20,
        )

        self.assertFalse(signal.ready)
        self.assertEqual(signal.state, "missing_demo_evidence")

    def test_reviewer_accepts_structured_demo_evidence_covering_required_recording_count_marker(self) -> None:
        gate = self._gate_with_demo_requirement(
            _FakeGitHubClient(
                checks=[
                    WorkflowCheckSuite(name="CI", status="completed", conclusion="success"),
                    WorkflowCheckSuite(name="Security", status="completed", conclusion="success"),
                ],
                files=[
                    PullRequestFileChange(filename="orchestrator/core/reviewer.py", patch="+ change"),
                    PullRequestFileChange(filename="tests/test_reviewer_gate.py", patch="+ test"),
                ],
                review_body=(
                    "Good:\n- implemented\n\n"
                    "Risks:\n- low\n\n"
                    "Must-fix:\n- none\n\n"
                    "Tests:\n- pytest -q\n\n"
                    "Questions:\n- none\n\n"
                    "Follow-ups:\n- none\n\n"
                    "## Demo Evidence\n"
                    "<!-- master-builder:qa-demo-evidence v1 -->\n"
                    "<!-- master-builder:qa-demo-required-targets browser -->\n"
                    "<!-- master-builder:qa-demo-required-counts browser=2 -->\n"
                    "- Browser happy path [target=browser; reference=https://preview.example; "
                    "object_key=example/example-default/run-1/qa-demo-1.webm; "
                    "sha256=0000000000000000000000000000000000000000000000000000000000000001; "
                    "release_commit_sha=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb; "
                    "release_context_sha256=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa]: "
                    "https://cdn.example/qa-demos/example/example-default/run-1/qa-demo-1.webm\n"
                    "- Browser edge case [target=browser; reference=https://preview.example; "
                    "object_key=example/example-default/run-1/qa-demo-2.webm; "
                    "sha256=0000000000000000000000000000000000000000000000000000000000000002; "
                    "release_commit_sha=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb; "
                    "release_context_sha256=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa]: "
                    "https://cdn.example/qa-demos/example/example-default/run-1/qa-demo-2.webm\n"
                ),
            )
        )

        signal = gate.evaluate_pr(
            repo_full_name="example/repo",
            pr_number=21,
        )

        self.assertTrue(signal.ready)
        self.assertEqual(signal.state, "ready")

    def test_reviewer_blocks_structured_demo_evidence_that_does_not_match_persisted_run_recordings(self) -> None:
        gate = self._gate_with_demo_requirement(
            _FakeGitHubClient(
                checks=[
                    WorkflowCheckSuite(name="CI", status="completed", conclusion="success"),
                    WorkflowCheckSuite(name="Security", status="completed", conclusion="success"),
                ],
                files=[
                    PullRequestFileChange(filename="orchestrator/core/reviewer.py", patch="+ change"),
                    PullRequestFileChange(filename="tests/test_reviewer_gate.py", patch="+ test"),
                ],
                review_body=(
                    "Good:\n- implemented\n\n"
                    "Risks:\n- low\n\n"
                    "Must-fix:\n- none\n\n"
                    "Tests:\n- pytest -q\n\n"
                    "Questions:\n- none\n\n"
                    "Follow-ups:\n- none\n\n"
                    "## Demo Evidence\n"
                    "<!-- master-builder:qa-demo-evidence v1 -->\n"
                    "<!-- master-builder:qa-demo-required-targets browser -->\n"
                    "<!-- master-builder:qa-demo-required-counts browser=1 -->\n"
                    "- Browser happy path [target=browser; reference=https://preview.example; "
                    "object_key=example/example-default/run-1/qa-demo-1.webm; "
                    "sha256=0000000000000000000000000000000000000000000000000000000000000001; "
                    "release_commit_sha=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb; "
                    "release_context_sha256=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa]: "
                    "https://cdn.example/qa-demos/example/example-default/run-1/qa-demo-1.webm\n"
                ),
            ),
            expected_recordings=(
                {
                    "capture_target": "browser",
                    "object_key": "example/example-default/run-1/qa-demo-1.webm",
                    "content_sha256": "f" * 64,
                    "release_commit_sha": "b" * 40,
                    "release_context_sha256": "a" * 64,
                    "artifact_url": "https://cdn.example/qa-demos/example/example-default/run-1/qa-demo-1.webm",
                },
            ),
        )

        signal = gate.evaluate_pr(
            repo_full_name="example/repo",
            pr_number=21,
        )

        self.assertFalse(signal.ready)
        self.assertEqual(signal.state, "missing_demo_evidence")

    def test_reviewer_blocks_structured_demo_evidence_when_persisted_run_recordings_are_missing(self) -> None:
        gate = self._gate_with_demo_requirement(
            _FakeGitHubClient(
                checks=[
                    WorkflowCheckSuite(name="CI", status="completed", conclusion="success"),
                    WorkflowCheckSuite(name="Security", status="completed", conclusion="success"),
                ],
                files=[
                    PullRequestFileChange(filename="orchestrator/core/reviewer.py", patch="+ change"),
                    PullRequestFileChange(filename="tests/test_reviewer_gate.py", patch="+ test"),
                ],
                review_body=(
                    "Good:\n- implemented\n\n"
                    "Risks:\n- low\n\n"
                    "Must-fix:\n- none\n\n"
                    "Tests:\n- pytest -q\n\n"
                    "Questions:\n- none\n\n"
                    "Follow-ups:\n- none\n\n"
                    "## Demo Evidence\n"
                    "<!-- master-builder:qa-demo-evidence v1 -->\n"
                    "<!-- master-builder:qa-demo-required-targets browser -->\n"
                    "<!-- master-builder:qa-demo-required-counts browser=1 -->\n"
                    "- Browser happy path [target=browser; reference=https://preview.example; "
                    "object_key=example/example-default/run-1/qa-demo-1.webm; "
                    "sha256=0000000000000000000000000000000000000000000000000000000000000001; "
                    "release_commit_sha=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb; "
                    "release_context_sha256=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa]: "
                    "https://cdn.example/qa-demos/example/example-default/run-1/qa-demo-1.webm\n"
                ),
            ),
            expected_recordings=None,
            use_recordings_resolver=True,
        )

        signal = gate.evaluate_pr(
            repo_full_name="example/repo",
            pr_number=21,
        )

        self.assertFalse(signal.ready)
        self.assertEqual(signal.state, "missing_demo_evidence")

    def test_reviewer_accepts_structured_demo_evidence_that_matches_persisted_run_recordings(self) -> None:
        expected_recording = {
            "capture_target": "browser",
            "object_key": "example/example-default/run-1/qa-demo-1.webm",
            "content_sha256": "0" * 63 + "1",
            "release_commit_sha": "b" * 40,
            "release_context_sha256": "a" * 64,
            "artifact_url": "https://cdn.example/qa-demos/example/example-default/run-1/qa-demo-1.webm",
        }
        gate = self._gate_with_demo_requirement(
            _FakeGitHubClient(
                checks=[
                    WorkflowCheckSuite(name="CI", status="completed", conclusion="success"),
                    WorkflowCheckSuite(name="Security", status="completed", conclusion="success"),
                ],
                files=[
                    PullRequestFileChange(filename="orchestrator/core/reviewer.py", patch="+ change"),
                    PullRequestFileChange(filename="tests/test_reviewer_gate.py", patch="+ test"),
                ],
                review_body=(
                    "Good:\n- implemented\n\n"
                    "Risks:\n- low\n\n"
                    "Must-fix:\n- none\n\n"
                    "Tests:\n- pytest -q\n\n"
                    "Questions:\n- none\n\n"
                    "Follow-ups:\n- none\n\n"
                    "## Demo Evidence\n"
                    "<!-- master-builder:qa-demo-evidence v1 -->\n"
                    "<!-- master-builder:qa-demo-required-targets browser -->\n"
                    "<!-- master-builder:qa-demo-required-counts browser=1 -->\n"
                    "- Browser happy path [target=browser; reference=https://preview.example; "
                    "object_key=example/example-default/run-1/qa-demo-1.webm; "
                    "sha256=0000000000000000000000000000000000000000000000000000000000000001; "
                    "release_commit_sha=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb; "
                    "release_context_sha256=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa]: "
                    "https://cdn.example/qa-demos/example/example-default/run-1/qa-demo-1.webm\n"
                ),
            ),
            expected_recordings=(expected_recording,),
        )

        signal = gate.evaluate_pr(
            repo_full_name="example/repo",
            pr_number=21,
        )

        self.assertTrue(signal.ready)
        self.assertEqual(signal.state, "ready")

    def test_reviewer_blocks_structured_demo_evidence_from_another_project_scope(self) -> None:
        gate = self._gate_with_demo_requirement(
            _FakeGitHubClient(
                checks=[
                    WorkflowCheckSuite(name="CI", status="completed", conclusion="success"),
                    WorkflowCheckSuite(name="Security", status="completed", conclusion="success"),
                ],
                files=[
                    PullRequestFileChange(filename="orchestrator/core/reviewer.py", patch="+ change"),
                    PullRequestFileChange(filename="tests/test_reviewer_gate.py", patch="+ test"),
                ],
                review_body=(
                    "Good:\n- implemented\n\n"
                    "Risks:\n- low\n\n"
                    "Must-fix:\n- none\n\n"
                    "Tests:\n- pytest -q\n\n"
                    "Questions:\n- none\n\n"
                    "Follow-ups:\n- none\n\n"
                    "## Demo Evidence\n"
                    "<!-- master-builder:qa-demo-evidence v1 -->\n"
                    "<!-- master-builder:qa-demo-required-targets browser -->\n"
                    "<!-- master-builder:qa-demo-required-counts browser=1 -->\n"
                    "- Browser happy path [target=browser; reference=https://preview.example; "
                    "object_key=other-tenant/other-project/run-1/qa-demo-1.webm]: "
                    "https://cdn.example/qa-demos/other-tenant/other-project/run-1/qa-demo-1.webm\n"
                ),
            )
        )

        signal = gate.evaluate_pr(
            repo_full_name="example/repo",
            pr_number=22,
        )

        self.assertFalse(signal.ready)
        self.assertEqual(signal.state, "missing_demo_evidence")

    def test_reviewer_blocks_structured_demo_evidence_from_another_run_in_same_project(self) -> None:
        gate = self._gate_with_demo_requirement(
            _FakeGitHubClient(
                checks=[
                    WorkflowCheckSuite(name="CI", status="completed", conclusion="success"),
                    WorkflowCheckSuite(name="Security", status="completed", conclusion="success"),
                ],
                files=[
                    PullRequestFileChange(filename="orchestrator/core/reviewer.py", patch="+ change"),
                    PullRequestFileChange(filename="tests/test_reviewer_gate.py", patch="+ test"),
                ],
                review_body=(
                    "Good:\n- implemented\n\n"
                    "Risks:\n- low\n\n"
                    "Must-fix:\n- none\n\n"
                    "Tests:\n- pytest -q\n\n"
                    "Questions:\n- none\n\n"
                    "Follow-ups:\n- none\n\n"
                    "## Demo Evidence\n"
                    "<!-- master-builder:qa-demo-evidence v1 -->\n"
                    "<!-- master-builder:qa-demo-required-targets browser -->\n"
                    "<!-- master-builder:qa-demo-required-counts browser=1 -->\n"
                    "- Browser happy path [target=browser; reference=https://preview.example; "
                    "object_key=example/example-default/old-run/qa-demo-1.webm]: "
                    "https://cdn.example/qa-demos/example/example-default/old-run/qa-demo-1.webm\n"
                ),
            )
        )

        signal = gate.evaluate_pr(
            repo_full_name="example/repo",
            pr_number=23,
        )

        self.assertFalse(signal.ready)
        self.assertEqual(signal.state, "missing_demo_evidence")

    def test_reviewer_blocks_when_constructor_required_target_is_missing(self) -> None:
        gate = self._gate_with_demo_requirement(
            _FakeGitHubClient(
                checks=[
                    WorkflowCheckSuite(name="CI", status="completed", conclusion="success"),
                    WorkflowCheckSuite(name="Security", status="completed", conclusion="success"),
                ],
                files=[
                    PullRequestFileChange(filename="orchestrator/core/reviewer.py", patch="+ change"),
                    PullRequestFileChange(filename="tests/test_reviewer_gate.py", patch="+ test"),
                ],
                review_body=(
                    "Good:\n- implemented\n\n"
                    "Risks:\n- low\n\n"
                    "Must-fix:\n- none\n\n"
                    "Tests:\n- pytest -q\n\n"
                    "Questions:\n- none\n\n"
                    "Follow-ups:\n- none\n\n"
                    "## Demo Evidence\n"
                    "<!-- master-builder:qa-demo-evidence v1 -->\n"
                    "- Browser walkthrough [target=browser; reference=https://preview.example; "
                    "object_key=tenant-1/project-1/run-1/qa-demo-1.webm]: https://cdn.example/qa/browser.webm\n"
                ),
            ),
            required_targets=("browser", "ios"),
        )

        signal = gate.evaluate_pr(
            repo_full_name="example/repo",
            pr_number=20,
        )

        self.assertFalse(signal.ready)
        self.assertEqual(signal.state, "missing_demo_evidence")

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
