import unittest

from orchestrator.core.workflow.runner import PmPlan, WorkflowRequest, WorkflowResult, WorkflowRunner


class _ExecuteOnlyAgents:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: int = 0

    def execute(self, request: WorkflowRequest, *, test_feedback_hook=None, stage_checkpoint_hook=None) -> WorkflowResult:  # noqa: ANN001
        _ = (test_feedback_hook, stage_checkpoint_hook)
        self.calls += 1
        if self.fail:
            raise RuntimeError("boom")
        return WorkflowResult(
            succeeded=True,
            plan=PmPlan(
                plan_steps=["one-shot run"],
                acceptance_criteria=["open validated PR"],
                risks=["merge conflicts"],
            ),
            pr_url=f"https://github.com/example/repo/pull/{request.run_id}",
            summary=["workflow completed"],
            test_guidance=["pytest -q"],
            attempts=1,
        )


class WorkflowRunnerTests(unittest.TestCase):
    def _request(self) -> WorkflowRequest:
        return WorkflowRequest(
            tenant_id="tenant-a",
            run_id="run-1",
            issue_key="MAB-10",
            issue_summary="One-shot execution",
            issue_description="Execute one-shot workflow",
            max_dev_test_review_loops=1,
            suggested_test_commands=["pytest -q"],
        )

    def test_runner_delegates_to_execute_once(self) -> None:
        agents = _ExecuteOnlyAgents()
        result = WorkflowRunner(agents).run(self._request())

        self.assertTrue(result.succeeded)
        self.assertEqual(agents.calls, 1)
        self.assertEqual(result.attempts, 1)
        self.assertIn("workflow completed", result.summary)
        self.assertEqual(result.pr_url, "https://github.com/example/repo/pull/run-1")
        self.assertIsNone(result.diagnostics)

    def test_runner_wraps_execute_exceptions(self) -> None:
        agents = _ExecuteOnlyAgents(fail=True)
        result = WorkflowRunner(agents).run(self._request())

        self.assertFalse(result.succeeded)
        self.assertEqual(agents.calls, 1)
        self.assertIsNotNone(result.diagnostics)
        self.assertEqual(result.diagnostics.stage, "workflow")
        self.assertIn("Workflow execution failed: boom", result.diagnostics.message)
        self.assertIsNotNone(result.follow_up_issue)
        self.assertEqual(result.follow_up_issue["target_status"], "Backlog")


if __name__ == "__main__":
    unittest.main()
