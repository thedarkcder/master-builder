import unittest
from unittest.mock import patch
import json

from orchestrator.core.codex_agents import CodexWorkflowAgents, answer_board_question_with_codex
from orchestrator.core.codex_invocation import CodexInvocationContext
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
            suggested_test_commands=["python -m unittest"],
            execution_repo_dir="/tmp/test-repo",
        )

    def test_agents_map_json_payloads(self) -> None:
        runtime = CodexRuntime(
            model="gpt-5-codex",
            max_output_tokens=1200,
            command="override",
            _request=_RuntimeQueue(
                [
                    '{"plan_steps":["step1"],"acceptance_criteria":["ac1"],"risks":["risk1"],"next_stage":"dev"}',
                    '{"change_summary":["implemented"],"pr_url":"https://example/pull/1"}',
                    '{"passed":true,"guidance":["run tests"],"feedback":null}',
                    '{"approved":true,"summary":["looks good"],"feedback":null,"pr_url":"https://example/pull/1"}',
                ]
            ),
        )
        agents = CodexWorkflowAgents(runtime=runtime)
        request = self._request()

        with patch("orchestrator.core.codex_agents.render_prompt", side_effect=lambda template_name, **_: template_name):
            plan = agents.pm(request, 1, None, [], None, None, None)
            dev = agents.dev(request, plan, 1, None)
            test_result = agents.test(request, plan, dev, 1)
            review = agents.review(request, plan, dev, test_result, 1)

        self.assertEqual(plan.plan_steps, ["step1"])
        self.assertEqual(dev.pr_url, "https://example/pull/1")
        self.assertTrue(test_result.passed)
        self.assertTrue(review.approved)
        self.assertEqual(review.outcome, "approved")

    def test_review_fallback_does_not_treat_generic_not_as_rejection(self) -> None:
        runtime = CodexRuntime(
            model="gpt-5-codex",
            max_output_tokens=1200,
            command="override",
            _request=_RuntimeQueue(
                [
                    '{"plan_steps":["step1"],"acceptance_criteria":["ac1"],"risks":["risk1"],"next_stage":"dev"}',
                    '{"change_summary":["implemented"],"pr_url":"https://example/pull/1"}',
                    '{"passed":true,"guidance":["run tests"],"feedback":null}',
                    "approved, do not merge automatically",
                ]
            ),
        )
        agents = CodexWorkflowAgents(runtime=runtime)
        request = self._request()

        with patch("orchestrator.core.codex_agents.render_prompt", side_effect=lambda template_name, **_: template_name):
            plan = agents.pm(request, 1, None, [], None, None, None)
            dev = agents.dev(request, plan, 1, None)
            test_result = agents.test(request, plan, dev, 1)
            review = agents.review(request, plan, dev, test_result, 1)

        self.assertTrue(review.approved)
        self.assertEqual(review.outcome, "approved")

    def test_review_outcome_falls_back_to_blocked_when_feedback_indicates_hard_stop(self) -> None:
        runtime = CodexRuntime(
            model="gpt-5-codex",
            max_output_tokens=1200,
            command="override",
            _request=_RuntimeQueue(
                [
                    '{"plan_steps":["step1"],"acceptance_criteria":["ac1"],"risks":["risk1"],"next_stage":"dev"}',
                    '{"change_summary":["implemented"],"pr_url":"https://example/pull/1"}',
                    '{"passed":true,"guidance":["run tests"],"feedback":null}',
                    '{"approved":false,"summary":["Governed runtime unavailable"],"feedback":"Governed runtime unavailable"}',
                ]
            ),
        )
        agents = CodexWorkflowAgents(runtime=runtime)
        request = self._request()

        with patch("orchestrator.core.codex_agents.render_prompt", side_effect=lambda template_name, **_: template_name):
            plan = agents.pm(request, 1, None, [], None, None, None)
            dev = agents.dev(request, plan, 1, None)
            test_result = agents.test(request, plan, dev, 1)
            review = agents.review(request, plan, dev, test_result, 1)

        self.assertFalse(review.approved)
        self.assertEqual(review.outcome, "blocked")

    def test_pm_fallback_extracts_selected_macos_when_linux_also_present(self) -> None:
        runtime = CodexRuntime(
            model="gpt-5-codex",
            max_output_tokens=1200,
            command="override",
            _request=_RuntimeQueue(
                [
                    (
                        "Execution capability mismatch: PM selected macos but current worker is linux. "
                        "Requeue on worker:macos before dev/test/review."
                    ),
                ]
            ),
        )
        agents = CodexWorkflowAgents(runtime=runtime)
        request = self._request()

        with patch("orchestrator.core.codex_agents.render_prompt", side_effect=lambda template_name, **_: template_name):
            plan = agents.pm(request, 1, None, [], None, None, None)

        self.assertEqual(plan.execution_worker_capability, "macos")

    def test_answer_board_question(self) -> None:
        runtime = CodexRuntime(
            model="gpt-5-codex",
            max_output_tokens=1200,
            command="override",
            _request=lambda _system, _user, _working_dir=None, _on_log_line=None: '{"message":"2 blocked issues: MAB-1, MAB-2"}',
        )

        with patch("orchestrator.core.codex_agents.render_prompt", side_effect=lambda template_name, **_: template_name):
            message = answer_board_question_with_codex(
                runtime=runtime,
                question="what is blocked?",
                project_keys=["MAB"],
                issues=[{"key": "MAB-1", "summary": "A", "status": "Blocked"}],
                status_counts={"Blocked": 1},
                invocation_context=CodexInvocationContext(
                    channel="discord",
                    tenant_id="tenant-1",
                    project_id=None,
                    command="ask",
                    stage="answer",
                    working_dir="/tmp",
                ),
            )

        self.assertIn("MAB-1", message)

    def test_answer_board_question_ignores_history_in_prompt(self) -> None:
        runtime = CodexRuntime(
            model="gpt-5-codex",
            max_output_tokens=1200,
            command="override",
            _request=lambda _system, _user, _working_dir=None, _on_log_line=None: '{"message":"ok"}',
        )
        captured: dict = {}

        def _render_prompt(template_name: str, **kwargs) -> str:
            if template_name == "discord/ask_answer_user.j2":
                captured["history_json"] = kwargs.get("history_json")
            return template_name

        with (
            patch("orchestrator.core.codex_agents.render_prompt", side_effect=_render_prompt),
            patch("orchestrator.core.codex_agents.invoke_codex_json", return_value={"message": "ok"}),
        ):
            answer_board_question_with_codex(
                runtime=runtime,
                question="what is blocked?",
                project_keys=["MAB"],
                issues=[{"key": "MAB-1", "summary": "A", "status": "Blocked"}],
                status_counts={"Blocked": 1},
                invocation_context=CodexInvocationContext(
                    channel="discord",
                    tenant_id="tenant-1",
                    project_id=None,
                    command="ask",
                    stage="answer",
                    working_dir="/tmp",
                ),
                history=[{"question": "status?", "answer": "MAB-74 stale", "issue_key": "MAB-74"}],
            )

        self.assertEqual(json.loads(captured["history_json"]), [])

    def test_stage_log_sink_emits_payload(self) -> None:
        captured_logs: list[dict] = []

        def _request(_system: str, _user: str, _working_dir: str | None = None, _on_log_line=None) -> str:
            if _on_log_line is not None:
                _on_log_line("stdout", "line-1")
                _on_log_line("stderr", "line-2")
            return '{"plan_steps":["step1"],"acceptance_criteria":["ac1"],"risks":[],"next_stage":"dev"}'

        runtime = CodexRuntime(
            model="gpt-5-codex",
            max_output_tokens=1200,
            command="override",
            _request=_request,
        )
        agents = CodexWorkflowAgents(runtime=runtime, log_sink=lambda payload: captured_logs.append(payload))

        with patch("orchestrator.core.codex_agents.render_prompt", side_effect=lambda template_name, **_: template_name):
            plan = agents.pm(self._request(), 1, None, [], None, None, None)
        self.assertEqual(plan.plan_steps, ["step1"])
        self.assertEqual(len(captured_logs), 2)
        self.assertEqual(captured_logs[0]["stage"], "pm")
        self.assertEqual(captured_logs[0]["attempt"], 1)
        self.assertEqual(captured_logs[0]["stream"], "stdout")
        self.assertEqual(captured_logs[0]["message"], "line-1")
        self.assertEqual(captured_logs[1]["stream"], "stderr")

    def test_stage_log_sink_survives_log_persist_failure(self) -> None:
        captured_logs: list[dict] = []

        def _request(_system: str, _user: str, _working_dir: str | None = None, _on_log_line=None) -> str:
            if _on_log_line is not None:
                _on_log_line("stdout", "line-1")
            return '{"plan_steps":["step1"],"acceptance_criteria":["ac1"],"risks":[],"next_stage":"dev"}'

        runtime = CodexRuntime(
            model="gpt-5-codex",
            max_output_tokens=1200,
            command="override",
            _request=_request,
        )
        agents = CodexWorkflowAgents(runtime=runtime, log_sink=lambda payload: captured_logs.append(payload))

        with (
            patch("orchestrator.core.codex_agents.render_prompt", side_effect=lambda template_name, **_: template_name),
            patch("orchestrator.core.codex_invocation._enqueue_codex_log_line", side_effect=RuntimeError("db down")),
        ):
            plan = agents.pm(self._request(), 1, None, [], None, None, None)
        self.assertEqual(plan.plan_steps, ["step1"])
        self.assertEqual(len(captured_logs), 1)
        self.assertEqual(captured_logs[0]["message"], "line-1")


if __name__ == "__main__":
    unittest.main()
