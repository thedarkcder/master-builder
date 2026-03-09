import unittest
from unittest.mock import patch

from orchestrator.core.codex_runtime import CodexRuntime
from orchestrator.core.workflow.orchestrated_run_runner import OrchestratedRunWorkflowExecutor
from orchestrator.core.workflow.runner import WorkflowRequest


class _RuntimeNoop:
    def __call__(self, _system: str, _user: str, _working_dir: str | None = None, _on_log_line=None) -> str:
        return "{}"


class OneShotRunnerTests(unittest.TestCase):
    def _request(self) -> WorkflowRequest:
        return WorkflowRequest(
            tenant_id="tenant-1",
            run_id="run-1",
            issue_key="MAB-100",
            issue_summary="One-shot",
            issue_description="desc",
            max_dev_test_review_loops=1,
            suggested_test_commands=["pytest -q"],
            execution_repo_dir="/tmp/test-repo",
        )

    def _executor(self) -> OrchestratedRunWorkflowExecutor:
        runtime = CodexRuntime(
            model="gpt-5-codex",
            max_output_tokens=1200,
            command="override",
            _request=_RuntimeNoop(),
        )
        return OrchestratedRunWorkflowExecutor(runtime=runtime)

    def test_approved_payload_returns_success(self) -> None:
        with (
            patch("orchestrator.core.workflow.orchestrated_run_runner.render_prompt", side_effect=lambda name, **_: name),
            patch(
                "orchestrator.core.workflow.orchestrated_run_runner.invoke_codex_json",
                return_value={
                    "status": "approved",
                    "summary": ["done"],
                    "pr_url": "https://example/pull/1",
                    "review_findings": [],
                    "test_guidance": ["pytest -q"],
                },
            ),
        ):
            result = self._executor().execute(self._request())
        self.assertTrue(result.succeeded)
        self.assertEqual(result.pr_url, "https://example/pull/1")

    def test_needs_changes_returns_failure(self) -> None:
        with (
            patch("orchestrator.core.workflow.orchestrated_run_runner.render_prompt", side_effect=lambda name, **_: name),
            patch(
                "orchestrator.core.workflow.orchestrated_run_runner.invoke_codex_json",
                return_value={
                    "status": "needs_changes",
                    "summary": ["changes requested"],
                    "feedback": "Fix tests",
                    "pr_url": None,
                    "review_findings": ["tests failing"],
                },
            ),
        ):
            result = self._executor().execute(self._request())
        self.assertFalse(result.succeeded)
        self.assertIsNotNone(result.diagnostics)
        self.assertEqual(result.diagnostics.stage, "needs_changes")
        self.assertIn("Fix tests", result.diagnostics.message)


if __name__ == "__main__":
    unittest.main()
