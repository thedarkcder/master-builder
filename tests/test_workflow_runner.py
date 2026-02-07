import unittest

from orchestrator.core.workflow_runner import (
    DevResult,
    PmPlan,
    ReviewResult,
    TestResult,
    WorkflowRequest,
    WorkflowRunner,
)


class _FakeAgents:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.test_results_by_attempt: dict[int, TestResult] = {}
        self.review_results_by_attempt: dict[int, ReviewResult] = {}

    def pm(self, request: WorkflowRequest) -> PmPlan:
        self.calls.append("pm")
        return PmPlan(
            plan_steps=["analyze", "implement", "validate"],
            acceptance_criteria=["ship PR output", "include tests"],
            risks=["integration drift"],
        )

    def dev(
        self,
        request: WorkflowRequest,
        plan: PmPlan,
        attempt: int,
        feedback: str | None,
    ) -> DevResult:
        self.calls.append(f"dev:{attempt}:{feedback or '-'}")
        return DevResult(
            change_summary=[f"attempt {attempt} implementation"],
            pr_url=f"https://github.com/example/repo/pull/{attempt}",
        )

    def test(
        self,
        request: WorkflowRequest,
        plan: PmPlan,
        dev_result: DevResult,
        attempt: int,
    ) -> TestResult:
        self.calls.append(f"test:{attempt}")
        return self.test_results_by_attempt.get(
            attempt,
            TestResult(
                passed=True,
                guidance=request.suggested_test_commands or ["python3 -m unittest"],
                feedback=None,
            ),
        )

    def review(
        self,
        request: WorkflowRequest,
        plan: PmPlan,
        dev_result: DevResult,
        test_result: TestResult,
        attempt: int,
    ) -> ReviewResult:
        self.calls.append(f"review:{attempt}")
        return self.review_results_by_attempt.get(
            attempt,
            ReviewResult(
                approved=True,
                summary=["ready for PR"],
                feedback=None,
                pr_url=None,
            ),
        )


class WorkflowRunnerTests(unittest.TestCase):
    def _request(self, *, loops: int = 2, max_runtime_minutes: int = 30) -> WorkflowRequest:
        return WorkflowRequest(
            tenant_id="tenant-a",
            run_id="run-1",
            issue_key="MAB-10",
            issue_summary="Build workflow runner",
            issue_description=(
                "Objective: Build PM->Dev->Test->Review runner.\n"
                "Scope: in scope runner orchestration, out of scope deployment.\n"
                "Acceptance Criteria: run returns PR URL and diagnostics.\n"
                "Context: component=api, repo=master-builder.\n"
                "How to test: run unit tests.\n"
                "NFR intent: MVP.\n"
                "Risks: dependency on GitHub integration."
            ),
            max_dev_test_review_loops=loops,
            max_runtime_minutes=max_runtime_minutes,
            suggested_test_commands=["python3 -m unittest discover -s tests -p 'test_*.py'"],
        )

    def test_pipeline_executes_stages_in_order(self) -> None:
        agents = _FakeAgents()
        result = WorkflowRunner(agents).run(self._request())

        self.assertTrue(result.succeeded)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(
            agents.calls,
            ["pm", "dev:1:-", "test:1", "review:1"],
        )
        self.assertEqual(result.pr_url, "https://github.com/example/repo/pull/1")
        self.assertEqual(result.summary, ["ready for PR"])
        self.assertEqual(
            result.test_guidance,
            ["python3 -m unittest discover -s tests -p 'test_*.py'"],
        )
        self.assertIsNone(result.diagnostics)

    def test_loop_retries_until_test_passes_within_limit(self) -> None:
        agents = _FakeAgents()
        agents.test_results_by_attempt[1] = TestResult(
            passed=False,
            guidance=["fix failing tests"],
            feedback="unit tests failed",
        )
        result = WorkflowRunner(agents).run(self._request(loops=2))

        self.assertTrue(result.succeeded)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(
            agents.calls,
            [
                "pm",
                "dev:1:-",
                "test:1",
                "dev:2:unit tests failed",
                "test:2",
                "review:2",
            ],
        )

    def test_loop_retries_until_review_approves_within_limit(self) -> None:
        agents = _FakeAgents()
        agents.review_results_by_attempt[1] = ReviewResult(
            approved=False,
            summary=[],
            feedback="needs edge-case handling",
            pr_url=None,
        )
        result = WorkflowRunner(agents).run(self._request(loops=2))

        self.assertTrue(result.succeeded)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(
            agents.calls,
            [
                "pm",
                "dev:1:-",
                "test:1",
                "review:1",
                "dev:2:needs edge-case handling",
                "test:2",
                "review:2",
            ],
        )

    def test_failure_has_structured_diagnostics_after_max_attempts(self) -> None:
        agents = _FakeAgents()
        agents.test_results_by_attempt[1] = TestResult(
            passed=False,
            guidance=["rerun tests"],
            feedback="test failure attempt 1",
        )
        agents.test_results_by_attempt[2] = TestResult(
            passed=False,
            guidance=["rerun tests"],
            feedback="test failure attempt 2",
        )
        result = WorkflowRunner(agents).run(self._request(loops=2))

        self.assertFalse(result.succeeded)
        self.assertEqual(result.attempts, 2)
        self.assertIsNone(result.pr_url)
        self.assertIsNotNone(result.diagnostics)
        self.assertEqual(result.diagnostics.stage, "test")
        self.assertEqual(
            result.diagnostics.message,
            "Max workflow attempts reached after test failures",
        )
        self.assertEqual(result.diagnostics.attempts, 2)
        self.assertEqual(
            result.diagnostics.history,
            [
                {"stage": "test", "attempt": "1", "event": "test failure attempt 1"},
                {"stage": "test", "attempt": "2", "event": "test failure attempt 2"},
            ],
        )
        self.assertIsNotNone(result.follow_up_issue)
        self.assertEqual(result.follow_up_issue["target_status"], "Backlog")
        self.assertFalse(result.follow_up_issue["auto_promote"])
        self.assertIn("Why it matters", result.follow_up_issue["description"])

    def test_missing_pr_url_is_safe_failure(self) -> None:
        agents = _FakeAgents()

        def _dev_without_pr(
            request: WorkflowRequest,
            plan: PmPlan,
            attempt: int,
            feedback: str | None,
        ) -> DevResult:
            agents.calls.append(f"dev:{attempt}:{feedback or '-'}")
            return DevResult(change_summary=["no pr yet"], pr_url=None)

        agents.dev = _dev_without_pr  # type: ignore[method-assign]
        agents.review_results_by_attempt[1] = ReviewResult(
            approved=True,
            summary=["approved but missing PR"],
            feedback=None,
            pr_url=None,
        )

        result = WorkflowRunner(agents).run(self._request(loops=1))

        self.assertFalse(result.succeeded)
        self.assertIsNotNone(result.diagnostics)
        self.assertEqual(result.diagnostics.stage, "review")
        self.assertEqual(result.diagnostics.message, "Workflow succeeded but no PR URL was produced")

    def test_runtime_limit_returns_failure_diagnostics(self) -> None:
        agents = _FakeAgents()
        time_values = iter([0.0, 0.0, 61.0, 61.0, 61.0])
        runner = WorkflowRunner(agents, monotonic_fn=lambda: next(time_values))

        result = runner.run(self._request(loops=1, max_runtime_minutes=1))

        self.assertFalse(result.succeeded)
        self.assertIsNotNone(result.diagnostics)
        self.assertEqual(result.diagnostics.stage, "runtime")
        self.assertEqual(result.diagnostics.message, "Run exceeded max runtime of 1 minute(s)")

    def test_gtd_preflight_blocks_when_required_context_missing(self) -> None:
        agents = _FakeAgents()
        request = WorkflowRequest(
            tenant_id="tenant-a",
            run_id="run-2",
            issue_key="MAB-4",
            issue_summary="Implement GTD preflight",
            issue_description="Implement this quickly.",
            max_dev_test_review_loops=1,
            max_runtime_minutes=30,
            suggested_test_commands=["python3 -m unittest"],
        )

        result = WorkflowRunner(agents).run(request)

        self.assertFalse(result.succeeded)
        self.assertIsNotNone(result.diagnostics)
        self.assertEqual(result.diagnostics.stage, "preflight")
        self.assertIn("Good To Do validation failed", result.diagnostics.message)
        self.assertEqual(agents.calls, [])

    def test_placeholder_without_tracked_followup_blocks_with_draft_followup(self) -> None:
        agents = _FakeAgents()
        agents.review_results_by_attempt[1] = ReviewResult(
            approved=True,
            summary=["TODO: finish webhook validation for orchestrator/api/routes_webhook.py"],
            feedback=None,
            pr_url=None,
        )

        result = WorkflowRunner(agents).run(self._request(loops=1))

        self.assertFalse(result.succeeded)
        self.assertIsNotNone(result.diagnostics)
        self.assertEqual(result.diagnostics.stage, "review")
        self.assertIn("without tracked follow-up issue", result.diagnostics.message)
        self.assertIsNotNone(result.follow_up_issue)
        self.assertEqual(result.follow_up_issue["target_status"], "Backlog")
        self.assertIn("placeholder", result.follow_up_issue["labels"])
        self.assertIn("routes_webhook.py", result.follow_up_issue["description"])

    def test_placeholder_with_tracked_followup_still_blocks_without_new_draft(self) -> None:
        agents = _FakeAgents()
        agents.review_results_by_attempt[1] = ReviewResult(
            approved=True,
            summary=[
                "Temporary placeholder kept for compatibility.",
                "Tracked in MAB-777 with exact removal steps.",
            ],
            feedback=None,
            pr_url=None,
        )

        result = WorkflowRunner(agents).run(self._request(loops=1))

        self.assertFalse(result.succeeded)
        self.assertIsNotNone(result.diagnostics)
        self.assertEqual(result.diagnostics.stage, "review")
        self.assertIn("tracked follow-up issue(s) present", result.diagnostics.message)
        self.assertIsNone(result.follow_up_issue)
