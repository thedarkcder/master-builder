import unittest

from orchestrator.core.codex_agents import CodexWorkflowAgents, answer_board_question_with_codex
from orchestrator.core.codex_runtime import CodexRuntime
from orchestrator.core.workflow.runner import WorkflowRequest


class _RuntimeQueue:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = outputs

    def __call__(
        self,
        _system: str,
        _user: str,
        _working_dir: str | None = None,
        _on_log_line=None,
    ) -> str:
        if not self.outputs:
            return "{}"
        return self.outputs.pop(0)


class CodexWorkflowAgentsTests(unittest.TestCase):
    def _request(self) -> WorkflowRequest:
        return WorkflowRequest(
            tenant_id="tenant-1",
            run_id="run-1",
            issue_key="MAB-54",
            issue_summary="Integrate Codex runtime",
            issue_description="Objective and acceptance criteria",
            max_dev_test_review_loops=1,
            max_runtime_minutes=30,
            suggested_test_commands=["python -m unittest"],
            execution_repo_dir="/tmp/test-repo",
        )

    def test_agents_map_json_payloads(self) -> None:
        runtime = CodexRuntime(
            model="gpt-5-codex",
            timeout_seconds=30,
            max_output_tokens=1200,
            command="override",
            _request=_RuntimeQueue(
                [
                    '{"plan_steps":["step1"],"acceptance_criteria":["ac1"],"risks":["risk1"]}',
                    '{"change_summary":["implemented"],"pr_url":"https://example/pull/1"}',
                    '{"passed":true,"guidance":["run tests"],"feedback":null}',
                    '{"approved":true,"summary":["looks good"],"feedback":null,"pr_url":"https://example/pull/1"}',
                ]
            ),
        )
        agents = CodexWorkflowAgents(runtime=runtime)
        request = self._request()

        plan = agents.pm(request)
        dev = agents.dev(request, plan, 1, None)
        test_result = agents.test(request, plan, dev, 1)
        review = agents.review(request, plan, dev, test_result, 1)

        self.assertEqual(plan.plan_steps, ["step1"])
        self.assertEqual(dev.pr_url, "https://example/pull/1")
        self.assertTrue(test_result.passed)
        self.assertTrue(review.approved)

    def test_answer_board_question(self) -> None:
        runtime = CodexRuntime(
            model="gpt-5-codex",
            timeout_seconds=30,
            max_output_tokens=1200,
            command="override",
            _request=lambda _system, _user, _working_dir=None, _on_log_line=None: '{"message":"2 blocked issues: MAB-1, MAB-2"}',
        )

        message = answer_board_question_with_codex(
            runtime=runtime,
            question="what is blocked?",
            project_keys=["MAB"],
            issues=[{"key": "MAB-1", "summary": "A", "status": "Blocked"}],
            status_counts={"Blocked": 1},
        )

        self.assertIn("MAB-1", message)

    def test_stage_log_sink_emits_payload(self) -> None:
        captured_logs: list[dict] = []

        def _request(_system: str, _user: str, _working_dir: str | None = None, _on_log_line=None) -> str:
            if _on_log_line is not None:
                _on_log_line("stdout", "line-1")
                _on_log_line("stderr", "line-2")
            return '{"plan_steps":["step1"],"acceptance_criteria":["ac1"],"risks":[]}'

        runtime = CodexRuntime(
            model="gpt-5-codex",
            timeout_seconds=30,
            max_output_tokens=1200,
            command="override",
            _request=_request,
        )
        agents = CodexWorkflowAgents(runtime=runtime, log_sink=lambda payload: captured_logs.append(payload))

        plan = agents.pm(self._request())
        self.assertEqual(plan.plan_steps, ["step1"])
        self.assertEqual(len(captured_logs), 2)
        self.assertEqual(captured_logs[0]["stage"], "pm")
        self.assertEqual(captured_logs[0]["attempt"], 0)
        self.assertEqual(captured_logs[0]["stream"], "stdout")
        self.assertEqual(captured_logs[0]["message"], "line-1")
        self.assertEqual(captured_logs[1]["stream"], "stderr")


if __name__ == "__main__":
    unittest.main()
