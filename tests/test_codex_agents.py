import unittest
from unittest.mock import patch
import json

from orchestrator.core.codex_agents import (
    CodexWorkflowAgents,
    answer_board_question_with_runtime,
    answer_voice_room_persona_with_codex,
    route_voice_entry_with_runtime,
)
from orchestrator.core.runtime_invocation import AgentInvocationContext
from orchestrator.core.codex_runtime import CodexRuntime, CodexRuntimeError
from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot
from orchestrator.core.workflow.runner import (
    DevResult,
    PmPlan,
    ReviewResult,
    TestResult,
    WorkflowRequest,
    WorkflowStageCheckpoint,
)


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
            execution_branch="run/MAB-54/run-1",
            base_branch="main",
            integration_branch="feature/MAB-54",
            pr_target_branch="main",
        )

    def test_agents_map_json_payloads(self) -> None:
        runtime = CodexRuntime(
            model="gpt-5-codex",
            max_output_tokens=1200,
            command="override",
            _request=_RuntimeQueue(
                [
                    '{"outcome":"continue","plan_steps":["step1"],"acceptance_criteria":["ac1"],"risks":["risk1"],"next_stage":"dev","execution_worker_capability":"linux"}',
                    '{"outcome":"continue","change_summary":["implemented"],"pr_url":"https://example/pull/1","blocker_message":null}',
                    '{"outcome":"continue","guidance":["run tests"],"feedback":null,"blocker_message":null}',
                    '{"outcome":"continue","summary":["looks good"],"feedback":null,"pr_url":"https://example/pull/1","blocker_message":null}',
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
        self.assertEqual(test_result.outcome, "continue")
        self.assertEqual(review.outcome, "continue")

    def test_pm_maps_compact_evidence_ledger_fields(self) -> None:
        runtime = CodexRuntime(
            model="gpt-5-codex",
            max_output_tokens=1200,
            command="override",
            _request=_RuntimeQueue(
                [
                    (
                        '{"outcome":"blocked","plan_steps":["step1"],"acceptance_criteria":["ac1"],"risks":["risk1"],'
                        '"next_stage":"dev","execution_worker_capability":"linux","blocker_message":"Apple developer access still pending",'
                        '"resolved_prerequisites":["Supabase redirect URI approved"],'
                        '"unresolved_prerequisites":["Provision staging Service ID"]}'
                    ),
                ]
            ),
        )
        agents = CodexWorkflowAgents(runtime=runtime)

        with patch("orchestrator.core.codex_agents.render_prompt", side_effect=lambda template_name, **_: template_name):
            plan = agents.pm(self._request(), 1, None, [], None, None, None)

        self.assertEqual(plan.outcome, "blocked")
        self.assertEqual(plan.blocker_message, "Apple developer access still pending")
        self.assertEqual(plan.resolved_prerequisites, ["Supabase redirect URI approved"])
        self.assertEqual(plan.unresolved_prerequisites, ["Provision staging Service ID"])

    def test_pm_prompt_includes_project_metadata_in_context(self) -> None:
        runtime = CodexRuntime(
            model="gpt-5-codex",
            max_output_tokens=1200,
            command="override",
            _request=_RuntimeQueue(
                [
                    '{"outcome":"continue","plan_steps":["step1"],"acceptance_criteria":["ac1"],"risks":[],"next_stage":"dev","execution_worker_capability":"linux"}',
                ]
            ),
        )
        agents = CodexWorkflowAgents(runtime=runtime)
        request = WorkflowRequest(
            tenant_id="tenant-1",
            project_id="project-1",
            project_name="Route25 App",
            github_repository="https://github.com/example/repo",
            jira_project_key="GP",
            run_id="run-1",
            issue_key="MAB-54",
            issue_summary="Integrate Codex runtime",
            issue_description="Objective and acceptance criteria",
            max_dev_test_review_loops=1,
            suggested_test_commands=["python -m unittest"],
            execution_repo_dir="/tmp/test-repo",
            execution_branch="run/MAB-54/run-1",
            base_branch="main",
            integration_branch="feature/MAB-54",
            pr_target_branch="main",
        )
        captured: dict[str, object] = {}

        def _render_prompt(template_name: str, **kwargs) -> str:
            if template_name == "workflow/pm_user.j2":
                captured.update(kwargs)
            return template_name

        with patch("orchestrator.core.codex_agents.render_prompt", side_effect=_render_prompt):
            agents.pm(request, 1, None, [], None, None, None)

        self.assertEqual(captured["project_id"], "project-1")
        self.assertEqual(captured["project_name"], "Route25 App")
        self.assertEqual(captured["github_repository"], "https://github.com/example/repo")
        self.assertEqual(captured["jira_project_key"], "GP")
        self.assertEqual(captured["execution_repo_dir"], "/tmp/test-repo")
        self.assertEqual(captured["execution_branch"], "run/MAB-54/run-1")
        self.assertEqual(captured["integration_branch"], "feature/MAB-54")
        self.assertEqual(captured["allow_pr_creation"], "false")
        allowed_tools = json.loads(str(captured["allowed_tools_json"]))
        decision_tool = next(item for item in allowed_tools if item["tool_name"] == "decision.read_state")
        self.assertEqual(decision_tool["category"], "decision")
        self.assertIn("Decision Gate", decision_tool["description"])
        self.assertNotIn("agent_tool_command", captured)

    def test_test_prompt_receives_structured_tool_catalog_with_descriptions(self) -> None:
        runtime = CodexRuntime(
            model="gpt-5-codex",
            max_output_tokens=1200,
            command="override",
            _request=_RuntimeQueue(
                [
                    '{"outcome":"continue","plan_steps":["step1"],"acceptance_criteria":["ac1"],"risks":[],"next_stage":"dev","execution_worker_capability":"linux"}',
                    '{"outcome":"continue","change_summary":["implemented"],"pr_url":null,"blocker_message":null}',
                    '{"outcome":"continue","guidance":["run tests"],"feedback":null,"blocker_message":null}',
                ]
            ),
        )
        agents = CodexWorkflowAgents(runtime=runtime)
        request = self._request()
        captured: dict[str, object] = {}

        def _render_prompt(template_name: str, **kwargs) -> str:
            if template_name == "workflow/test_user.j2":
                captured.update(kwargs)
            return template_name

        with patch("orchestrator.core.codex_agents.render_prompt", side_effect=_render_prompt):
            plan = agents.pm(request, 1, None, [], None, None, None)
            dev = agents.dev(request, plan, 1, None)
            agents.test(request, plan, dev, 1)

        allowed_tools = json.loads(str(captured["allowed_tools_json"]))
        runtime_tool = next(item for item in allowed_tools if item["tool_name"] == "project.get_runtime_values")
        self.assertEqual(runtime_tool["category"], "project")
        self.assertIn("project-configured runtime values by key", runtime_tool["description"])
        self.assertIn("source of truth", runtime_tool["description"])

    def test_stage_prompts_include_answered_human_inputs(self) -> None:
        runtime = CodexRuntime(
            model="gpt-5-codex",
            max_output_tokens=1200,
            command="override",
            _request=_RuntimeQueue(
                [
                    '{"outcome":"continue","plan_steps":["step1"],"acceptance_criteria":["ac1"],"risks":[],"next_stage":"dev","execution_worker_capability":"linux"}',
                    '{"outcome":"continue","change_summary":["implemented"],"pr_url":null,"blocker_message":null}',
                ]
            ),
        )
        agents = CodexWorkflowAgents(runtime=runtime)
        request = WorkflowRequest(
            tenant_id="tenant-1",
            project_id="project-1",
            project_name="Route25 App",
            github_repository="https://github.com/example/repo",
            jira_project_key="GP",
            run_id="run-1",
            issue_key="MAB-54",
            issue_summary="Integrate Codex runtime",
            issue_description="Objective and acceptance criteria",
            max_dev_test_review_loops=1,
            suggested_test_commands=["python -m unittest"],
            execution_repo_dir="/tmp/test-repo",
            execution_branch="run/MAB-54/run-1",
            base_branch="main",
            integration_branch="feature/MAB-54",
            pr_target_branch="main",
            allow_pr_creation=True,
            human_inputs=[
                {
                    "request_id": "request-1",
                    "request_type": "verification_code",
                    "prompt": "Reply with the Apple code",
                    "value": "123456",
                }
            ],
        )
        captured: dict[str, object] = {}

        def _render_prompt(template_name: str, **kwargs) -> str:
            if template_name == "workflow/dev_user.j2":
                captured.update(kwargs)
            return template_name

        with patch("orchestrator.core.codex_agents.render_prompt", side_effect=_render_prompt):
            plan = agents.pm(request, 1, None, [], None, None, None)
            agents.dev(request, plan, 1, None)

        self.assertEqual(json.loads(str(captured["human_inputs_json"]))[0]["value"], "123456")
        self.assertEqual(captured["allow_pr_creation"], "true")
        self.assertIn("value", json.loads(str(captured["human_inputs_json"]))[0])
        self.assertNotIn("agent_tool_command", captured)

    def test_review_prompt_includes_allow_pr_creation(self) -> None:
        runtime = CodexRuntime(
            model="gpt-5-codex",
            max_output_tokens=1200,
            command="override",
            _request=_RuntimeQueue(
                [
                    '{"outcome":"continue","plan_steps":["step1"],"acceptance_criteria":["ac1"],"risks":["risk1"],"next_stage":"dev","execution_worker_capability":"linux"}',
                    '{"outcome":"continue","change_summary":["implemented"],"pr_url":"https://example/pull/1","blocker_message":null}',
                    '{"outcome":"continue","guidance":["run tests"],"feedback":null,"blocker_message":null}',
                    '{"outcome":"continue","summary":["looks good"],"feedback":null,"pr_url":"https://example/pull/1","blocker_message":null}',
                ]
            ),
        )
        agents = CodexWorkflowAgents(runtime=runtime)
        request = WorkflowRequest(
            tenant_id="tenant-1",
            project_id="project-1",
            project_name="Route25 App",
            github_repository="https://github.com/example/repo",
            jira_project_key="GP",
            run_id="run-1",
            issue_key="MAB-54",
            issue_summary="Integrate Codex runtime",
            issue_description="Objective and acceptance criteria",
            max_dev_test_review_loops=1,
            suggested_test_commands=["python -m unittest"],
            execution_repo_dir="/tmp/test-repo",
            execution_branch="run/MAB-54/run-1",
            base_branch="main",
            integration_branch="feature/MAB-54",
            pr_target_branch="main",
            allow_pr_creation=True,
        )
        captured: dict[str, object] = {}

        def _render_prompt(template_name: str, **kwargs) -> str:
            if template_name == "workflow/review_user.j2":
                captured.update(kwargs)
            return template_name

        with patch("orchestrator.core.codex_agents.render_prompt", side_effect=_render_prompt):
            plan = agents.pm(request, 1, None, [], None, None, None)
            dev = agents.dev(request, plan, 1, None)
            test_result = agents.test(request, plan, dev, 1)
            agents.review(request, plan, dev, test_result, 1)

        self.assertEqual(captured["allow_pr_creation"], "true")
        self.assertNotIn("agent_tool_command", captured)

    def test_resume_session_id_is_applied_to_selected_stage(self) -> None:
        runtime = CodexRuntime(
            model="gpt-5-codex",
            max_output_tokens=1200,
            command="override",
            _request=_RuntimeQueue([]),
        )
        agents = CodexWorkflowAgents(runtime=runtime)
        request = WorkflowRequest(
            tenant_id="tenant-1",
            run_id="run-1",
            issue_key="MAB-54",
            issue_summary="Integrate Codex runtime",
            issue_description="Objective and acceptance criteria",
            max_dev_test_review_loops=1,
            suggested_test_commands=["python -m unittest"],
            execution_repo_dir="/tmp/test-repo",
            execution_branch="run/MAB-54/run-1",
            base_branch="main",
            integration_branch="feature/MAB-54",
            pr_target_branch="main",
            entry_mode="resume",
            entry_stage="dev",
            checkpoint_kind="execution",
            checkpoint_session_id="dev-session-123",
        )
        captured_contexts: list[AgentInvocationContext] = []

        def _invoke_runtime_json(*, context, **kwargs):  # noqa: ANN001
            _ = kwargs
            captured_contexts.append(context)
            if context.stage == "pm":
                return {
                    "outcome": "continue",
                    "plan_steps": ["step1"],
                    "acceptance_criteria": ["ac1"],
                    "risks": [],
                    "next_stage": "dev",
                    "execution_worker_capability": "linux",
                }
            return {"outcome": "continue", "change_summary": ["implemented"], "pr_url": None}

        with (
            patch("orchestrator.core.codex_agents.render_prompt", side_effect=lambda template_name, **_: template_name),
            patch("orchestrator.core.codex_agents.invoke_runtime_json_with_tools", side_effect=_invoke_runtime_json),
        ):
            plan = agents.pm(request, 1, None, [], None, None, None)
            agents.dev(request, plan, 1, None)

        self.assertEqual(captured_contexts[0].stage, "pm")
        self.assertIsNone(captured_contexts[0].codex_session_id)
        self.assertEqual(captured_contexts[1].stage, "dev")
        self.assertEqual(captured_contexts[1].codex_session_id, "dev-session-123")

    def test_review_resume_includes_previous_review_feedback_in_prompt(self) -> None:
        runtime = CodexRuntime(
            model="gpt-5-codex",
            max_output_tokens=1200,
            command="override",
            _request=_RuntimeQueue([]),
        )
        agents = CodexWorkflowAgents(runtime=runtime)
        resume_snapshot = ExecutionSnapshot.empty()
        resume_snapshot.apply_stage_checkpoint(
            WorkflowStageCheckpoint(
                stage="pm",
                attempt=1,
                status="completed",
                summary="PM ready",
                plan=PmPlan(plan_steps=["step"], acceptance_criteria=["ac"], risks=[]),
            )
        )
        resume_snapshot.apply_stage_checkpoint(
            WorkflowStageCheckpoint(
                stage="review",
                attempt=1,
                status="blocked",
                summary="Needs nonce verification",
                review_result=ReviewResult(
                    summary=["Needs nonce verification"],
                    feedback="Verify the nonce flow with the QA account",
                ),
            )
        )
        request = WorkflowRequest(
            tenant_id="tenant-1",
            run_id="run-1",
            issue_key="MAB-54",
            issue_summary="Integrate Codex runtime",
            issue_description="Objective and acceptance criteria",
            max_dev_test_review_loops=1,
            suggested_test_commands=["python -m unittest"],
            execution_repo_dir="/tmp/test-repo",
            execution_branch="run/MAB-54/run-1",
            base_branch="main",
            integration_branch="feature/MAB-54",
            pr_target_branch="main",
            entry_mode="resume",
            entry_stage="review",
            checkpoint_kind="execution",
            checkpoint_session_id="dev-session-123",
            checkpoint_payload=resume_snapshot.dump(),
        )
        captured: dict[str, object] = {}

        def _render_prompt(template_name: str, **kwargs) -> str:
            if template_name == "workflow/review_user.j2":
                captured.update(kwargs)
            return template_name

        with (
            patch("orchestrator.core.codex_agents.render_prompt", side_effect=_render_prompt),
            patch("orchestrator.core.codex_agents.invoke_runtime_json_with_tools", return_value={"outcome": "continue", "summary": ["ok"], "feedback": None, "pr_url": None}),
        ):
            agents.review(
                request,
                PmPlan(plan_steps=["step"], acceptance_criteria=["ac"], risks=[]),
                DevResult(change_summary=["implemented"], pr_url=None),
                TestResult(guidance=["python -m unittest"]),
                1,
            )

        self.assertEqual(captured["previous_review_summary_json"], "[\"Needs nonce verification\"]")
        self.assertEqual(captured["previous_review_feedback"], "Verify the nonce flow with the QA account")

    def test_dev_test_and_review_map_structured_blockers(self) -> None:
        runtime = CodexRuntime(
            model="gpt-5-codex",
            max_output_tokens=1200,
            command="override",
            _request=_RuntimeQueue(
                [
                    '{"outcome":"continue","plan_steps":["step1"],"acceptance_criteria":["ac1"],"risks":["risk1"],"next_stage":"dev","execution_worker_capability":"linux"}',
                    '{"outcome":"blocked","change_summary":["implemented"],"pr_url":null,"blocker_message":"APPLE_TEST_PASSWORD missing"}',
                    '{"outcome":"failed","guidance":["retry targeted UI test"],"feedback":"UI test failed","blocker_message":"xcodebuild missing"}',
                    '{"outcome":"blocked","summary":["Awaiting approval"],"feedback":"Need PM approval","blocker_message":"Decision owner approval missing"}',
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

        self.assertEqual(dev.outcome, "blocked")
        self.assertEqual(dev.blocker_message, "APPLE_TEST_PASSWORD missing")
        self.assertEqual(test_result.outcome, "failed")
        self.assertEqual(test_result.blocker_message, "xcodebuild missing")
        self.assertEqual(review.outcome, "blocked")
        self.assertEqual(review.blocker_message, "Decision owner approval missing")

    def test_review_requires_explicit_outcome(self) -> None:
        runtime = CodexRuntime(
            model="gpt-5-codex",
            max_output_tokens=1200,
            command="override",
            _request=_RuntimeQueue(
                [
                    '{"outcome":"continue","plan_steps":["step1"],"acceptance_criteria":["ac1"],"risks":["risk1"],"next_stage":"dev","execution_worker_capability":"linux"}',
                    '{"outcome":"continue","change_summary":["implemented"],"pr_url":"https://example/pull/1"}',
                    '{"outcome":"continue","guidance":["run tests"],"feedback":null}',
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
            with self.assertRaises(CodexRuntimeError):
                agents.review(request, plan, dev, test_result, 1)

    def test_review_outcome_uses_explicit_blocked_contract(self) -> None:
        runtime = CodexRuntime(
            model="gpt-5-codex",
            max_output_tokens=1200,
            command="override",
            _request=_RuntimeQueue(
                [
                    '{"outcome":"continue","plan_steps":["step1"],"acceptance_criteria":["ac1"],"risks":["risk1"],"next_stage":"dev","execution_worker_capability":"linux"}',
                    '{"outcome":"continue","change_summary":["implemented"],"pr_url":"https://example/pull/1"}',
                    '{"outcome":"continue","guidance":["run tests"],"feedback":null}',
                    '{"outcome":"blocked","summary":["Governed runtime unavailable"],"feedback":"Governed runtime unavailable","blocker_message":"Governed runtime unavailable"}',
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

        self.assertEqual(review.outcome, "blocked")

    def test_pm_requires_explicit_outcome(self) -> None:
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
            with self.assertRaises(CodexRuntimeError):
                agents.pm(request, 1, None, [], None, None, None)

    def test_answer_board_question(self) -> None:
        runtime = CodexRuntime(
            model="gpt-5-codex",
            max_output_tokens=1200,
            command="override",
            _request=lambda _system, _user, _working_dir=None, _on_log_line=None: '{"message":"2 blocked issues: MAB-1, MAB-2"}',
        )

        with patch("orchestrator.core.codex_agents.render_prompt", side_effect=lambda template_name, **_: template_name):
            message = answer_board_question_with_runtime(
                runtime=runtime,
                question="what is blocked?",
                project_keys=["MAB"],
                issues=[{"key": "MAB-1", "summary": "A", "status": "Blocked"}],
                status_counts={"Blocked": 1},
                invocation_context=AgentInvocationContext(
                    channel="discord",
                    tenant_id="tenant-1",
                    project_id=None,
                    command="ask",
                    stage="answer",
                    working_dir="/tmp",
                ),
            )

        self.assertIn("MAB-1", message)

    def test_answer_board_question_passes_history_to_prompt(self) -> None:
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

        sample_history = [{"question": "status?", "answer": "MAB-74 stale", "issue_key": "MAB-74"}]
        with (
            patch("orchestrator.core.codex_agents.render_prompt", side_effect=_render_prompt),
            patch(
                "orchestrator.core.codex_agents._invoke_discord_json_maybe_tools",
                return_value={"message": "ok"},
            ),
        ):
            answer_board_question_with_runtime(
                runtime=runtime,
                question="what is blocked?",
                project_keys=["MAB"],
                issues=[{"key": "MAB-1", "summary": "A", "status": "Blocked"}],
                status_counts={"Blocked": 1},
                invocation_context=AgentInvocationContext(
                    channel="discord",
                    tenant_id="tenant-1",
                    project_id=None,
                    command="ask",
                    stage="answer",
                    working_dir="/tmp",
                ),
                history=sample_history,
            )

        self.assertEqual(json.loads(captured["history_json"]), sample_history)

    def test_answer_board_question_passes_persona_id_to_ask_answer_prompts(self) -> None:
        runtime = CodexRuntime(
            model="gpt-5-codex",
            max_output_tokens=1200,
            command="override",
            _request=lambda _system, _user, _working_dir=None, _on_log_line=None: '{"message":"ok"}',
        )
        calls: list[tuple[str, dict]] = []

        def _render_prompt(template_name: str, **kwargs) -> str:
            calls.append((template_name, dict(kwargs)))
            return template_name

        with (
            patch("orchestrator.core.codex_agents.render_prompt", side_effect=_render_prompt),
            patch(
                "orchestrator.core.codex_agents._invoke_discord_json_maybe_tools",
                return_value={"message": "ok"},
            ),
        ):
            answer_board_question_with_runtime(
                runtime=runtime,
                question="any risks?",
                project_keys=["MAB"],
                issues=[],
                status_counts={},
                invocation_context=AgentInvocationContext(
                    channel="discord",
                    tenant_id="tenant-1",
                    project_id=None,
                    command="ask",
                    stage="answer",
                    working_dir="/tmp",
                ),
                answer_persona_id="security",
            )

        system_call = next(c for c in calls if c[0] == "discord/ask_answer_system.j2")
        user_call = next(c for c in calls if c[0] == "discord/ask_answer_user.j2")
        self.assertEqual(system_call[1].get("persona_id"), "security")
        self.assertEqual(user_call[1].get("persona_id"), "security")

    def test_voice_entry_interview_lane_normalizes_persona_to_pm(self) -> None:
        runtime = CodexRuntime(
            model="gpt-5-codex",
            max_output_tokens=1200,
            command="override",
            _request=_RuntimeQueue(
                [
                    '{"lane":"interview","persona":"architect","confidence":0.9,"reason":"brief"}',
                ]
            ),
        )
        ctx = AgentInvocationContext(
            channel="discord",
            tenant_id="tenant-1",
            project_id="project-1",
            command="router",
            stage="voice-entry-router",
            working_dir="/tmp",
        )
        with patch("orchestrator.core.codex_agents.render_prompt", side_effect=lambda template_name, **_: template_name):
            payload = route_voice_entry_with_runtime(
                runtime=runtime,
                transcript="We should define MVP scope",
                entry_source="unit-test",
                invocation_context=ctx,
            )
        self.assertEqual(payload["lane"], "interview")
        self.assertEqual(payload["persona"], "pm")

    def test_stage_log_sink_emits_payload(self) -> None:
        captured_logs: list[dict] = []

        def _request(_system: str, _user: str, _working_dir: str | None = None, _on_log_line=None) -> str:
            if _on_log_line is not None:
                _on_log_line("stdout", "line-1")
                _on_log_line("stderr", "line-2")
            return '{"outcome":"continue","plan_steps":["step1"],"acceptance_criteria":["ac1"],"risks":[],"next_stage":"dev","execution_worker_capability":"linux"}'

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
            return '{"outcome":"continue","plan_steps":["step1"],"acceptance_criteria":["ac1"],"risks":[],"next_stage":"dev","execution_worker_capability":"linux"}'

        runtime = CodexRuntime(
            model="gpt-5-codex",
            max_output_tokens=1200,
            command="override",
            _request=_request,
        )
        agents = CodexWorkflowAgents(runtime=runtime, log_sink=lambda payload: captured_logs.append(payload))

        with (
            patch("orchestrator.core.codex_agents.render_prompt", side_effect=lambda template_name, **_: template_name),
            patch("orchestrator.core.runtime_invocation._enqueue_runtime_log_line", side_effect=RuntimeError("db down")),
        ):
            plan = agents.pm(self._request(), 1, None, [], None, None, None)
        self.assertEqual(plan.plan_steps, ["step1"])
        self.assertEqual(len(captured_logs), 1)
        self.assertEqual(captured_logs[0]["message"], "line-1")

    def test_voice_entry_router_normalizes_invalid_lane_and_persona(self) -> None:
        runtime = CodexRuntime(
            model="gpt-5-codex",
            max_output_tokens=1200,
            command="override",
            _request=_RuntimeQueue(
                [
                    '{"lane":"unknown","persona":"bogus","confidence":-1,"reason":"x"}',
                ]
            ),
        )
        ctx = AgentInvocationContext(
            channel="discord",
            tenant_id="tenant-1",
            project_id="project-1",
            command="router",
            stage="voice-entry-router",
            working_dir="/tmp",
        )
        with patch("orchestrator.core.codex_agents.render_prompt", side_effect=lambda template_name, **_: template_name):
            payload = route_voice_entry_with_runtime(
                runtime=runtime,
                transcript="What is the status?",
                entry_source="unit-test",
                invocation_context=ctx,
            )
        self.assertEqual(payload["lane"], "ask")
        self.assertEqual(payload["persona"], "pm")
        self.assertEqual(payload["confidence"], 0.0)

    def test_voice_entry_router_legacy_persona_lane_maps_to_ask(self) -> None:
        runtime = CodexRuntime(
            model="gpt-5-codex",
            max_output_tokens=1200,
            command="override",
            _request=_RuntimeQueue(
                [
                    '{"lane":"persona","persona":"alien","confidence":0.5,"reason":"y"}',
                ]
            ),
        )
        ctx = AgentInvocationContext(
            channel="discord",
            tenant_id="tenant-1",
            project_id="project-1",
            command="router",
            stage="voice-entry-router",
            working_dir="/tmp",
        )
        with patch("orchestrator.core.codex_agents.render_prompt", side_effect=lambda template_name, **_: template_name):
            payload = route_voice_entry_with_runtime(
                runtime=runtime,
                transcript="Help me scope this",
                entry_source="unit-test",
                invocation_context=ctx,
            )
        self.assertEqual(payload["lane"], "ask")
        self.assertEqual(payload["persona"], "pm")

    def test_voice_entry_router_pm_lane_maps_to_interview(self) -> None:
        runtime = CodexRuntime(
            model="gpt-5-codex",
            max_output_tokens=1200,
            command="override",
            _request=_RuntimeQueue(
                [
                    '{"lane":"pm","persona":"pm","confidence":0.9,"reason":"brief"}',
                ]
            ),
        )
        ctx = AgentInvocationContext(
            channel="discord",
            tenant_id="tenant-1",
            project_id="project-1",
            command="router",
            stage="voice-entry-router",
            working_dir="/tmp",
        )
        with patch("orchestrator.core.codex_agents.render_prompt", side_effect=lambda template_name, **_: template_name):
            payload = route_voice_entry_with_runtime(
                runtime=runtime,
                transcript="We should build a dashboard",
                entry_source="unit-test",
                invocation_context=ctx,
            )
        self.assertEqual(payload["lane"], "interview")
        self.assertEqual(payload["persona"], "pm")

    def test_voice_room_persona_answer_accepts_message_only_schema(self) -> None:
        runtime = CodexRuntime(
            model="gpt-5-codex",
            max_output_tokens=1200,
            command="override",
            _request=_RuntimeQueue(
                [
                    '{"message":"The main integration risk is Discord attachment churn."}',
                ]
            ),
        )

        with patch("orchestrator.core.codex_agents.render_prompt", side_effect=lambda template_name, **_: template_name):
            payload = answer_voice_room_persona_with_codex(
                runtime=runtime,
                persona_id="architect",
                transcript="What is the main risk?",
                project_keys=["MAB"],
                issues=[{"key": "MAB-174"}],
                status_counts={"To Do": 1},
                invocation_context=AgentInvocationContext(
                    channel="discord",
                    tenant_id="tenant-1",
                    project_id="project-1",
                    command="pm",
                    stage="voice-room-architect",
                    working_dir="/tmp/test-repo",
                ),
                history=[{"question": "hi", "answer": "hello"}],
                github_context={"repository": "repo"},
            )

        self.assertEqual(payload["message"], "The main integration risk is Discord attachment churn.")
        self.assertEqual(payload["brief"], {})


if __name__ == "__main__":
    unittest.main()
