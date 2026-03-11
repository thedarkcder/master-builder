import unittest
from dataclasses import replace

from orchestrator.core.codex_runtime import CodexRuntime
from orchestrator.core.workflow.orchestrated_run_runner import OrchestratedRunWorkflowExecutor
from orchestrator.core.workflow.runner import DevResult, PmPlan, ReviewResult, TestResult, WorkflowRequest


class _RuntimeNoop:
    def __call__(self, _system: str, _user: str, _working_dir: str | None = None, _on_log_line=None) -> str:
        return "{}"


class _StubStageAgents:
    def __init__(
        self,
        *,
        plan: PmPlan,
        dev_results: list[DevResult],
        test_results: list[TestResult],
        review_results: list[ReviewResult],
    ) -> None:
        self.plan = plan
        self.dev_results = list(dev_results)
        self.test_results = list(test_results)
        self.review_results = list(review_results)
        self.dev_feedback: list[str | None] = []
        self.pm_calls = 0
        self.dev_calls = 0
        self.test_calls = 0
        self.review_calls = 0

    def pm(self, request, attempt, feedback, history, last_dev_result, last_test_result, last_review_result):  # noqa: ANN001
        _ = (request, attempt, feedback, history, last_dev_result, last_test_result, last_review_result)
        self.pm_calls += 1
        return self.plan

    def dev(self, request, plan, attempt, feedback):  # noqa: ANN001
        _ = (request, plan, attempt)
        self.dev_calls += 1
        self.dev_feedback.append(feedback)
        return self.dev_results.pop(0)

    def test(self, request, plan, dev_result, attempt):  # noqa: ANN001
        _ = (request, plan, dev_result, attempt)
        self.test_calls += 1
        return self.test_results.pop(0)

    def review(self, request, plan, dev_result, test_result, attempt):  # noqa: ANN001
        _ = (request, plan, dev_result, test_result, attempt)
        self.review_calls += 1
        return self.review_results.pop(0)


class OrchestratedRunRunnerTests(unittest.TestCase):
    def _request(self) -> WorkflowRequest:
        return WorkflowRequest(
            tenant_id="tenant-1",
            run_id="run-1",
            issue_key="MAB-100",
            issue_summary="Orchestrated run",
            issue_description="desc",
            max_dev_test_review_loops=2,
            suggested_test_commands=["pytest -q"],
            execution_repo_dir="/tmp/test-repo",
        )

    def _executor(self, stage_agents: _StubStageAgents) -> OrchestratedRunWorkflowExecutor:
        runtime = CodexRuntime(
            model="gpt-5-codex",
            max_output_tokens=1200,
            command="override",
            _request=_RuntimeNoop(),
        )
        return OrchestratedRunWorkflowExecutor(runtime=runtime, stage_agents=stage_agents)

    def test_review_needs_changes_loops_back_to_dev_until_approved(self) -> None:
        stage_agents = _StubStageAgents(
            plan=PmPlan(plan_steps=["plan"], acceptance_criteria=["ac1"], risks=[]),
            dev_results=[
                DevResult(change_summary=["implemented attempt 1"], pr_url="https://example/pull/1"),
                DevResult(change_summary=["implemented attempt 2"], pr_url="https://example/pull/1"),
            ],
            test_results=[
                TestResult(passed=True, guidance=["pytest -q"]),
                TestResult(passed=True, guidance=["pytest -q"]),
            ],
            review_results=[
                ReviewResult(
                    approved=False,
                    outcome="needs_changes",
                    summary=["Fix Apple nonce handling"],
                    feedback="Fix Apple nonce handling",
                    pr_url="https://example/pull/1",
                ),
                ReviewResult(
                    approved=True,
                    outcome="approved",
                    summary=["Looks good"],
                    feedback=None,
                    pr_url="https://example/pull/1",
                ),
            ],
        )

        result = self._executor(stage_agents).execute(self._request())

        self.assertTrue(result.succeeded)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(stage_agents.pm_calls, 1)
        self.assertEqual(stage_agents.dev_calls, 2)
        self.assertEqual(stage_agents.test_calls, 2)
        self.assertEqual(stage_agents.review_calls, 2)
        self.assertEqual(stage_agents.dev_feedback, [None, "Fix Apple nonce handling"])
        self.assertEqual(result.pr_url, "https://example/pull/1")

    def test_test_failure_retries_and_emits_feedback_hook(self) -> None:
        stage_agents = _StubStageAgents(
            plan=PmPlan(plan_steps=["plan"], acceptance_criteria=["ac1"], risks=[]),
            dev_results=[
                DevResult(change_summary=["implemented attempt 1"], pr_url=None),
                DevResult(change_summary=["implemented attempt 2"], pr_url="https://example/pull/2"),
            ],
            test_results=[
                TestResult(passed=False, guidance=["pytest -q"], feedback="A unit test failed"),
                TestResult(passed=True, guidance=["pytest -q"]),
            ],
            review_results=[
                ReviewResult(
                    approved=True,
                    outcome="approved",
                    summary=["Looks good"],
                    feedback=None,
                    pr_url="https://example/pull/2",
                ),
            ],
        )
        captured_feedback: list[tuple[int, str]] = []

        result = self._executor(stage_agents).execute(
            self._request(),
            test_feedback_hook=lambda attempt, feedback: captured_feedback.append((attempt, feedback)),
        )

        self.assertTrue(result.succeeded)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(captured_feedback, [(1, "A unit test failed")])
        self.assertEqual(stage_agents.dev_feedback, [None, "A unit test failed"])

    def test_blocked_review_stops_without_consuming_remaining_loops(self) -> None:
        stage_agents = _StubStageAgents(
            plan=PmPlan(plan_steps=["plan"], acceptance_criteria=["ac1"], risks=[]),
            dev_results=[DevResult(change_summary=["implemented"], pr_url=None)],
            test_results=[TestResult(passed=True, guidance=["pytest -q"])],
            review_results=[
                ReviewResult(
                    approved=False,
                    outcome="blocked",
                    summary=["Governed runtime unavailable"],
                    feedback="Governed runtime unavailable",
                    pr_url=None,
                )
            ],
        )

        result = self._executor(stage_agents).execute(self._request())

        self.assertFalse(result.succeeded)
        self.assertIsNotNone(result.diagnostics)
        self.assertEqual(result.diagnostics.stage, "review")
        self.assertEqual(result.attempts, 1)
        self.assertEqual(stage_agents.dev_calls, 1)
        self.assertEqual(stage_agents.review_calls, 1)

    def test_pm_capability_mismatch_returns_requeueable_failure(self) -> None:
        stage_agents = _StubStageAgents(
            plan=PmPlan(
                plan_steps=["plan"],
                acceptance_criteria=["ac1"],
                risks=[],
                execution_worker_capability="macos",
            ),
            dev_results=[],
            test_results=[],
            review_results=[],
        )

        result = self._executor(stage_agents).execute(
            replace(self._request(), current_worker_capability="linux", available_worker_capabilities=["linux"])
        )

        self.assertFalse(result.succeeded)
        self.assertIsNotNone(result.diagnostics)
        self.assertEqual(result.diagnostics.stage, "pm")
        self.assertIn("Execution capability mismatch:", result.diagnostics.message)
        self.assertEqual(stage_agents.dev_calls, 0)

    def test_pm_missing_evidence_stops_before_dev(self) -> None:
        stage_agents = _StubStageAgents(
            plan=PmPlan(
                plan_steps=["plan"],
                acceptance_criteria=["ac1"],
                risks=[],
                missing_evidence_sources=["decision_state", "knowledge"],
            ),
            dev_results=[],
            test_results=[],
            review_results=[],
        )

        result = self._executor(stage_agents).execute(self._request())

        self.assertFalse(result.succeeded)
        self.assertIsNotNone(result.diagnostics)
        self.assertEqual(result.diagnostics.stage, "pm")
        self.assertEqual(result.diagnostics.classification, "missing_context")
        self.assertIn("decision_state, knowledge", result.diagnostics.message)
        self.assertEqual(stage_agents.dev_calls, 0)

    def test_pm_confirmed_external_blocker_stops_before_dev(self) -> None:
        stage_agents = _StubStageAgents(
            plan=PmPlan(
                plan_steps=["plan"],
                acceptance_criteria=["ac1"],
                risks=[],
                confirmed_external_blockers=["Apple developer access is not approved for staging"],
            ),
            dev_results=[],
            test_results=[],
            review_results=[],
        )

        result = self._executor(stage_agents).execute(self._request())

        self.assertFalse(result.succeeded)
        self.assertIsNotNone(result.diagnostics)
        self.assertEqual(result.diagnostics.stage, "pm")
        self.assertEqual(result.diagnostics.classification, "external_blocker")
        self.assertIn("Apple developer access is not approved for staging", result.diagnostics.message)
        self.assertEqual(stage_agents.dev_calls, 0)


if __name__ == "__main__":
    unittest.main()
