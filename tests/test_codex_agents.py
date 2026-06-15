import os
from tempfile import TemporaryDirectory
import unittest
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
import json
import pytest

from orchestrator.core.runtime.agents import (
    CodexWorkflowAgents,
    _native_selector_catalog,
    answer_board_question_with_runtime,
    answer_voice_room_persona_with_runtime,
    route_voice_entry_with_runtime,
)
from orchestrator.core.runtime.invocation import AgentInvocationContext, RuntimeJsonContractError
from orchestrator.core.runtime.runtime import CodexRuntime, CodexRuntimeError
from orchestrator.core.config import get_settings
from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot
from orchestrator.core.workflow.runner import (
    DemoRequirement,
    DevResult,
    PmPlan,
    QaResult,
    ReviewResult,
    TestResult,
    WorkflowRequest,
    WorkflowStageCheckpoint,
)
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import Tenant


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
    _CAPTURE_TARGET_CONSTRAINTS_JSON = (
        '[{"capture_target":"browser","provider_available":true,"required_worker_platform":null,'
        '"availability_reason":"Browser capture uses the built-in Playwright recorder and requires a preview website URL at QA time."},'
        '{"capture_target":"ios","provider_available":true,"required_worker_platform":"macos",'
        '"availability_reason":"iOS capture requires the built-in or configured native recorder on a macOS worker."},'
        '{"capture_target":"android","provider_available":true,"required_worker_platform":"linux",'
        '"availability_reason":"Android capture requires the built-in or configured native recorder on a Linux Android worker."},'
        '{"capture_target":"desktop","provider_available":false,"required_worker_platform":null,'
        '"availability_reason":"Desktop capture provider is unavailable because no desktop recorder command is configured."}]'
    )

    def setUp(self) -> None:
        self._original_database_url = os.environ.get("ORCHESTRATOR_DATABASE_URL")
        self.temp_dir = TemporaryDirectory()
        self.database_url = f"sqlite:///{self.temp_dir.name}/codex_agents.db"
        os.environ["ORCHESTRATOR_DATABASE_URL"] = self.database_url
        get_settings.cache_clear()
        reset_db_engine_cache()
        run_migrations(database_url=self.database_url)
        session_factory = create_session_factory(database_url=self.database_url)
        with session_factory() as session:
            now = datetime.now(timezone.utc)
            session.add(
                Tenant(
                    tenant_id="tenant-1",
                    name="Tenant 1",
                    is_enabled=True,
                    jira_config={},
                    github_config={},
                    repos_config={},
                    policy_config={},
                    discord_config=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        if self._original_database_url is None:
            os.environ.pop("ORCHESTRATOR_DATABASE_URL", None)
        else:
            os.environ["ORCHESTRATOR_DATABASE_URL"] = self._original_database_url
        get_settings.cache_clear()
        reset_db_engine_cache()

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
                    '{"outcome":"continue","plan_steps":["step1"],"acceptance_criteria":["ac1"],"risks":["risk1"],"demo_requirements":[{"title":"Demo step1","acceptance_criterion":"ac1","capture_target":"browser","variants":["Invalid input is rejected","Repeat action remains safe"]}],"next_stage":"dev","execution_worker_capability":"linux"}',
                    '{"outcome":"continue","change_summary":["implemented"],"pr_url":"https://example/pull/1","blocker_message":null}',
                    '{"outcome":"continue","guidance":["run tests"],"validation_scope":"targeted_only","feedback":null,"blocker_message":null}',
                    '{"outcome":"continue","summary":["looks good"],"feedback":null,"pr_url":"https://example/pull/1","blocker_message":null}',
                    (
                        '{"outcome":"continue","summary":["Recorded demos"],"feedback":null,"blocker_message":null,'
                        '"scenarios":[{"name":"Happy path","objective":"Show feature","capture_target":"browser","start_path":"/",'
                        '"expected_outcomes":["Feature visible"],'
                        '"steps":[{"action":"goto","value":"/"},{"action":"assert_visible","selector":"text=Feature"}]}],'
                        '"recordings":[]}'
                    ),
                ]
            ),
        )
        agents = CodexWorkflowAgents(runtime=runtime)
        request = self._request()

        with patch("orchestrator.core.runtime.agents.render_prompt", side_effect=lambda template_name, **_: template_name):
            plan = agents.pm(request, 1, None, [], None, None, None, self._CAPTURE_TARGET_CONSTRAINTS_JSON)
            dev = agents.dev(request, plan, 1, None)
            test_result = agents.test(request, plan, dev, 1)
            review = agents.review(request, plan, dev, test_result, 1)
            qa = agents.qa(
                request,
                plan,
                dev,
                test_result,
                review,
                "https://preview.example",
                '[{"capture_target":"browser","capture_reference":"https://preview.example"}]',
                1,
            )

        self.assertEqual(plan.plan_steps, ["step1"])
        self.assertEqual(plan.demo_requirements[0].title, "Demo step1")
        self.assertEqual(dev.pr_url, "https://example/pull/1")
        self.assertEqual(test_result.outcome, "continue")
        self.assertEqual(review.outcome, "continue")
        self.assertIsInstance(qa, QaResult)
        self.assertEqual(qa.scenarios[0].name, "Happy path")

    def test_pm_maps_compact_evidence_ledger_fields(self) -> None:
        runtime = CodexRuntime(
            model="gpt-5-codex",
            max_output_tokens=1200,
            command="override",
            _request=_RuntimeQueue(
                [
                    (
                        '{"outcome":"blocked","plan_steps":["step1"],"acceptance_criteria":["ac1"],"risks":["risk1"],'
                        '"demo_requirements":[{"title":"Demo step1","acceptance_criterion":"ac1","capture_target":"browser","variants":["Invalid input is rejected","Repeat action remains safe"]}],'
                        '"next_stage":"dev","execution_worker_capability":"linux","blocker_message":"Apple developer access still pending",'
                        '"resolved_prerequisites":["Supabase redirect URI approved"],'
                        '"unresolved_prerequisites":["Provision staging Service ID"]}'
                    ),
                ]
            ),
        )
        agents = CodexWorkflowAgents(runtime=runtime)

        with patch("orchestrator.core.runtime.agents.render_prompt", side_effect=lambda template_name, **_: template_name):
            plan = agents.pm(
                self._request(),
                1,
                None,
                [],
                None,
                None,
                None,
                self._CAPTURE_TARGET_CONSTRAINTS_JSON,
            )

        self.assertEqual(plan.outcome, "blocked")
        self.assertEqual(plan.blocker_message, "Apple developer access still pending")
        self.assertEqual(plan.resolved_prerequisites, ["Supabase redirect URI approved"])
        self.assertEqual(plan.unresolved_prerequisites, ["Provision staging Service ID"])

    def test_pm_accepts_mixed_platform_demo_requirements_without_forcing_execution_worker(self) -> None:
        runtime = CodexRuntime(
            model="gpt-5-codex",
            max_output_tokens=1200,
            command="override",
            _request=_RuntimeQueue(
                [
                    (
                        '{"outcome":"continue","plan_steps":["step1"],"acceptance_criteria":["ac1"],"risks":[],"demo_requirements":'
                        '[{"title":"Browser demo","acceptance_criterion":"ac1","capture_target":"browser","variants":["Invalid input is rejected"]},'
                        '{"title":"iOS demo","acceptance_criterion":"ac1","capture_target":"ios","variants":["Repeat action remains safe"]},'
                        '{"title":"Android demo","acceptance_criterion":"ac1","capture_target":"android","variants":["Offline state is handled"]}],'
                        '"next_stage":"dev","execution_worker_capability":"linux"}'
                    ),
                ]
            ),
        )
        agents = CodexWorkflowAgents(runtime=runtime)

        with patch("orchestrator.core.runtime.agents.render_prompt", side_effect=lambda template_name, **_: template_name):
            plan = agents.pm(self._request(), 1, None, [], None, None, None, self._CAPTURE_TARGET_CONSTRAINTS_JSON)

        self.assertEqual(plan.execution_worker_capability, "linux")
        self.assertEqual(
            [requirement.capture_target for requirement in plan.demo_requirements],
            ["browser", "ios", "android"],
        )

    def test_pm_rejects_executable_plan_missing_explicit_ticket_demo_targets(self) -> None:
        runtime = CodexRuntime(
            model="gpt-5-codex",
            max_output_tokens=1200,
            command="override",
            _request=_RuntimeQueue(
                [
                    (
                        '{"outcome":"continue","plan_steps":["step1"],"acceptance_criteria":["ac1"],"risks":[],"demo_requirements":'
                        '[{"title":"Browser demo","acceptance_criterion":"ac1","capture_target":"browser","variants":["Invalid input is rejected"]}],'
                        '"next_stage":"dev","execution_worker_capability":"linux"}'
                    ),
                ]
            ),
        )
        agents = CodexWorkflowAgents(runtime=runtime)
        request = replace(
            self._request(),
            issue_description=(
                "Acceptance criteria: the delivered feature must be demoed in browser, iOS, and Android."
            ),
        )

        with patch("orchestrator.core.runtime.agents.render_prompt", side_effect=lambda template_name, **_: template_name):
            with self.assertRaisesRegex(
                CodexRuntimeError,
                "missing required demo capture target\\(s\\): android, ios",
            ):
                agents.pm(request, 1, None, [], None, None, None, self._CAPTURE_TARGET_CONSTRAINTS_JSON)

    def test_pm_rejects_executable_plan_missing_project_demo_targets(self) -> None:
        runtime = CodexRuntime(
            model="gpt-5-codex",
            max_output_tokens=1200,
            command="override",
            _request=_RuntimeQueue(
                [
                    (
                        '{"outcome":"continue","plan_steps":["step1"],"acceptance_criteria":["ac1"],"risks":[],"demo_requirements":'
                        '[{"title":"Browser demo","acceptance_criterion":"ac1","capture_target":"browser","variants":["Invalid input is rejected"]}],'
                        '"next_stage":"dev","execution_worker_capability":"linux"}'
                    ),
                ]
            ),
        )
        agents = CodexWorkflowAgents(runtime=runtime)
        request = replace(self._request(), project_demo_capture_targets=("ios", "android"))

        with patch("orchestrator.core.runtime.agents.render_prompt", side_effect=lambda template_name, **_: template_name):
            with self.assertRaisesRegex(
                CodexRuntimeError,
                "missing required demo capture target\\(s\\): android, ios",
            ):
                agents.pm(request, 1, None, [], None, None, None, self._CAPTURE_TARGET_CONSTRAINTS_JSON)

    def test_pm_rejects_unavailable_desktop_demo_requirements(self) -> None:
        runtime = CodexRuntime(
            model="gpt-5-codex",
            max_output_tokens=1200,
            command="override",
            _request=_RuntimeQueue(
                [
                    (
                        '{"outcome":"continue","plan_steps":["step1"],"acceptance_criteria":["ac1"],"risks":[],"demo_requirements":'
                        '[{"title":"Desktop demo","acceptance_criterion":"ac1","capture_target":"desktop","variants":["Repeat action remains safe"]}],'
                        '"next_stage":"dev","execution_worker_capability":"linux"}'
                    ),
                ]
            ),
        )
        agents = CodexWorkflowAgents(runtime=runtime)

        with patch("orchestrator.core.runtime.agents.render_prompt", side_effect=lambda template_name, **_: template_name):
            with self.assertRaisesRegex(CodexRuntimeError, "selected unavailable capture target 'desktop'"):
                agents.pm(self._request(), 1, None, [], None, None, None, self._CAPTURE_TARGET_CONSTRAINTS_JSON)

    def test_pm_accepts_desktop_demo_requirements_without_forcing_execution_worker(self) -> None:
        constraints = (
            '[{"capture_target":"desktop","provider_available":true,"required_worker_platform":"macos",'
            '"availability_reason":"Desktop capture uses the configured desktop recorder command."}]'
        )
        runtime = CodexRuntime(
            model="gpt-5-codex",
            max_output_tokens=1200,
            command="override",
            _request=_RuntimeQueue(
                [
                    (
                        '{"outcome":"continue","plan_steps":["step1"],"acceptance_criteria":["ac1"],"risks":[],"demo_requirements":'
                        '[{"title":"Desktop demo","acceptance_criterion":"ac1","capture_target":"desktop","variants":["Repeat action remains safe"]}],'
                        '"next_stage":"dev","execution_worker_capability":"linux"}'
                    ),
                ]
            ),
        )
        agents = CodexWorkflowAgents(runtime=runtime)

        with patch("orchestrator.core.runtime.agents.render_prompt", side_effect=lambda template_name, **_: template_name):
            plan = agents.pm(self._request(), 1, None, [], None, None, None, constraints)

        self.assertEqual(plan.execution_worker_capability, "linux")
        self.assertEqual(plan.demo_requirements[0].capture_target, "desktop")

    def test_pm_prompt_includes_project_metadata_in_context(self) -> None:
        runtime = CodexRuntime(
            model="gpt-5-codex",
            max_output_tokens=1200,
            command="codex",
            _request=_RuntimeQueue(
                [
                    '{"outcome":"continue","plan_steps":["step1"],"acceptance_criteria":["ac1"],"risks":[],"demo_requirements":[{"title":"Demo step1","acceptance_criterion":"ac1","capture_target":"browser","variants":["Invalid input is rejected","Repeat action remains safe"]}],"next_stage":"dev","execution_worker_capability":"linux"}',
                ]
            ),
        )
        agents = CodexWorkflowAgents(runtime=runtime)
        request = WorkflowRequest(
            tenant_id="tenant-1",
            project_id="project-1",
            project_name="example App",
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
            project_demo_capture_targets=("browser",),
        )
        captured: dict[str, object] = {}

        def _render_prompt(template_name: str, **kwargs) -> str:
            if template_name == "workflow/pm_user.j2":
                captured.update(kwargs)
            return template_name

        with patch("orchestrator.core.runtime.agents.render_prompt", side_effect=_render_prompt):
            agents.pm(request, 1, None, [], None, None, None, self._CAPTURE_TARGET_CONSTRAINTS_JSON)

        self.assertEqual(captured["project_id"], "project-1")
        self.assertEqual(captured["project_name"], "example App")
        self.assertEqual(captured["github_repository"], "https://github.com/example/repo")
        self.assertEqual(captured["jira_project_key"], "GP")
        self.assertEqual(captured["execution_repo_dir"], "/tmp/test-repo")
        self.assertEqual(captured["execution_branch"], "run/MAB-54/run-1")
        self.assertEqual(captured["integration_branch"], "feature/MAB-54")
        self.assertEqual(captured["allow_pr_creation"], "false")
        self.assertEqual(captured["project_demo_capture_targets_json"], '["browser"]')
        self.assertEqual(captured["qa_capture_target_constraints_json"], self._CAPTURE_TARGET_CONSTRAINTS_JSON)
        governed_tools = json.loads(str(captured["governed_tools_json"]))
        native_tools = json.loads(str(captured["native_tools_json"]))
        decision_tool = next(item for item in governed_tools if item["tool_name"] == "decision.read_state")
        self.assertEqual(decision_tool["category"], "decision")
        self.assertIn("Decision Gate", decision_tool["description"])
        self.assertNotIn("web.search", {item["tool_name"] for item in governed_tools})
        self.assertEqual({item["tool_name"] for item in native_tools}, {"web.search", "web.fetch", "browser.open", "browser.snapshot"})
        self.assertNotIn("agent_tool_command", captured)

    def test_pm_requires_demo_requirements(self) -> None:
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

        with patch("orchestrator.core.runtime.agents.render_prompt", side_effect=lambda template_name, **_: template_name):
            with self.assertRaisesRegex(CodexRuntimeError, "demo_requirements"):
                agents.pm(self._request(), 1, None, [], None, None, None, self._CAPTURE_TARGET_CONSTRAINTS_JSON)

    def test_pm_requires_explicit_demo_capture_target(self) -> None:
        runtime = CodexRuntime(
            model="gpt-5-codex",
            max_output_tokens=1200,
            command="override",
            _request=_RuntimeQueue(
                [
                    '{"outcome":"continue","plan_steps":["step1"],"acceptance_criteria":["ac1"],"risks":[],"demo_requirements":[{"title":"Demo step1","acceptance_criterion":"ac1"}],"next_stage":"dev","execution_worker_capability":"linux"}',
                ]
            ),
        )
        agents = CodexWorkflowAgents(runtime=runtime)

        with patch("orchestrator.core.runtime.agents.render_prompt", side_effect=lambda template_name, **_: template_name):
            with self.assertRaisesRegex(CodexRuntimeError, "demo_requirements"):
                agents.pm(self._request(), 1, None, [], None, None, None, self._CAPTURE_TARGET_CONSTRAINTS_JSON)

    def test_pm_requires_demo_requirement_variants(self) -> None:
        runtime = CodexRuntime(
            model="gpt-5-codex",
            max_output_tokens=1200,
            command="override",
            _request=_RuntimeQueue(
                [
                    '{"outcome":"continue","plan_steps":["step1"],"acceptance_criteria":["ac1"],"risks":[],"demo_requirements":[{"title":"Demo step1","acceptance_criterion":"ac1","capture_target":"browser"}],"next_stage":"dev","execution_worker_capability":"linux"}',
                ]
            ),
        )
        agents = CodexWorkflowAgents(runtime=runtime)

        with patch("orchestrator.core.runtime.agents.render_prompt", side_effect=lambda template_name, **_: template_name):
            with self.assertRaisesRegex(CodexRuntimeError, "variants"):
                agents.pm(self._request(), 1, None, [], None, None, None, self._CAPTURE_TARGET_CONSTRAINTS_JSON)

    def test_native_selector_catalog_extracts_ids_and_text_anchors(self) -> None:
        repo_dir = Path(self.temp_dir.name) / "repo"
        (repo_dir / "App").mkdir(parents=True, exist_ok=True)
        (repo_dir / "App" / "View.swift").write_text(
            'Text("Next")\n.accessibilityIdentifier("onboarding_tabview")\n.accessibilityIdentifier("start_demo_button")\n',
            encoding="utf-8",
        )
        (repo_dir / "GirlPowerUITests").mkdir(parents=True, exist_ok=True)
        (repo_dir / "GirlPowerUITests" / "GirlPowerUITests.swift").write_text(
            'assertVisible(selector: "text=Start Free Demo")\nassertVisible(selector: "id=onboarding_tabview")\nlet nextButton = app.buttons["Next"]\nlet continueButton = app.buttons["Continue"]\n',
            encoding="utf-8",
        )

        catalog = _native_selector_catalog(str(repo_dir))

        self.assertIn("onboarding_tabview", catalog["accessibility_ids"])
        self.assertIn("start_demo_button", catalog["accessibility_ids"])
        self.assertIn("Next", catalog["text_anchors"])
        self.assertIn("Continue", catalog["text_anchors"])
        self.assertIn("Start Free Demo", catalog["text_anchors"])

    def test_qa_prompt_includes_native_selector_catalog_and_rejects_invented_mobile_selector(self) -> None:
        runtime = CodexRuntime(
            model="gpt-5-codex",
            max_output_tokens=1200,
            command="override",
            _request=_RuntimeQueue(
                [
                    (
                        '{"outcome":"continue","summary":["Recorded demos"],"feedback":null,"blocker_message":null,'
                        '"scenarios":[{"name":"Happy path","objective":"Show feature","capture_target":"ios","start_path":"/",'
                        '"expected_outcomes":["Feature visible"],'
                        '"steps":[{"action":"assert_visible","selector":"id=onboarding.carousel"}]}],'
                        '"recordings":[]}'
                    ),
                ]
            ),
        )
        agents = CodexWorkflowAgents(runtime=runtime)
        repo_dir = Path(self.temp_dir.name) / "repo-qa"
        (repo_dir / "Features").mkdir(parents=True, exist_ok=True)
        (repo_dir / "Features" / "Onboarding.swift").write_text(
            '.accessibilityIdentifier("onboarding_tabview")\nText("Next")\n',
            encoding="utf-8",
        )
        request = replace(self._request(), execution_repo_dir=str(repo_dir))
        captured: dict[str, object] = {}

        def _render_prompt(template_name: str, **kwargs) -> str:
            if template_name == "workflow/qa_user.j2":
                captured.update(kwargs)
            return template_name

        with patch("orchestrator.core.runtime.agents.render_prompt", side_effect=_render_prompt):
            with self.assertRaisesRegex(CodexRuntimeError, "invented native accessibility identifier"):
                agents.qa(
                    request,
                    PmPlan(
                        plan_steps=["step1"],
                        acceptance_criteria=["ac1"],
                        risks=[],
                        demo_requirements=[
                            DemoRequirement(
                                title="Demo",
                                acceptance_criterion="ac1",
                                capture_target="ios",
                                variants=[],
                            )
                        ],
                    ),
                    DevResult(change_summary=["implemented"], pr_url="https://example/pull/1"),
                    TestResult(outcome="continue", guidance=["run tests"], feedback=None, blocker_message=None),
                    ReviewResult(
                        summary=["looks good"],
                        outcome="continue",
                        feedback=None,
                        pr_url="https://example/pull/1",
                        blocker_message=None,
                    ),
                    "",
                    '[{"capture_target":"ios","capture_reference":"ios-simulator://configured"}]',
                    1,
                )

        catalog = json.loads(str(captured["native_selector_catalog_json"]))
        self.assertIn("onboarding_tabview", catalog["accessibility_ids"])

    def test_qa_requires_explicit_scenario_capture_target(self) -> None:
        runtime = CodexRuntime(
            model="gpt-5-codex",
            max_output_tokens=1200,
            command="override",
            _request=_RuntimeQueue(
                [
                    (
                        '{"outcome":"continue","summary":["Recorded demos"],"feedback":null,"blocker_message":null,'
                        '"scenarios":[{"name":"Happy path","objective":"Show feature","start_path":"/",'
                        '"expected_outcomes":["Feature visible"],'
                        '"steps":[{"action":"goto","value":"/"},{"action":"assert_visible","selector":"text=Feature"}]}],'
                        '"recordings":[]}'
                    ),
                ]
            ),
        )
        agents = CodexWorkflowAgents(runtime=runtime)

        with patch("orchestrator.core.runtime.agents.render_prompt", side_effect=lambda template_name, **_: template_name):
            with self.assertRaisesRegex(CodexRuntimeError, "required scenario fields"):
                agents.qa(
                    self._request(),
                    PmPlan(
                        plan_steps=["step1"],
                        acceptance_criteria=["ac1"],
                        risks=[],
                        demo_requirements=[
                            DemoRequirement(
                                title="Demo",
                                acceptance_criterion="ac1",
                                capture_target="browser",
                                variants=[],
                            )
                        ],
                    ),
                    DevResult(change_summary=["implemented"], pr_url="https://example/pull/1"),
                    TestResult(outcome="continue", guidance=["run tests"], feedback=None, blocker_message=None),
                    ReviewResult(
                        summary=["looks good"],
                        outcome="continue",
                        feedback=None,
                        pr_url="https://example/pull/1",
                        blocker_message=None,
                    ),
                    "https://preview.example",
                    '[{"capture_target":"browser","capture_reference":"https://preview.example"}]',
                    1,
                )

    def test_test_prompt_receives_structured_tool_catalog_with_descriptions(self) -> None:
        runtime = CodexRuntime(
            model="gpt-5-codex",
            max_output_tokens=1200,
            command="codex",
            _request=_RuntimeQueue(
                [
                    '{"outcome":"continue","plan_steps":["step1"],"acceptance_criteria":["ac1"],"risks":[],"demo_requirements":[{"title":"Demo step1","acceptance_criterion":"ac1","capture_target":"browser","variants":["Invalid input is rejected","Repeat action remains safe"]}],"next_stage":"dev","execution_worker_capability":"linux"}',
                    '{"outcome":"continue","change_summary":["implemented"],"pr_url":null,"blocker_message":null}',
                    '{"outcome":"continue","guidance":["run tests"],"validation_scope":"targeted_only","feedback":null,"blocker_message":null}',
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

        with patch("orchestrator.core.runtime.agents.render_prompt", side_effect=_render_prompt):
            plan = agents.pm(request, 1, None, [], None, None, None)
            dev = agents.dev(request, plan, 1, None)
            agents.test(request, plan, dev, 1)

        governed_tools = json.loads(str(captured["governed_tools_json"]))
        native_tools = json.loads(str(captured["native_tools_json"]))
        runtime_tool = next(item for item in governed_tools if item["tool_name"] == "project.check_runtime_bindings")
        self.assertEqual(runtime_tool["category"], "project")
        self.assertIn("explicitly named project bindings", runtime_tool["description"])
        self.assertIn("never returns the underlying values", runtime_tool["description"])
        self.assertEqual({item["tool_name"] for item in native_tools}, {"web.search", "web.fetch", "browser.open", "browser.snapshot"})

    def test_test_prompt_receives_current_head_acceptance_context(self) -> None:
        runtime = CodexRuntime(
            model="gpt-5-codex",
            max_output_tokens=1200,
            command="codex",
            _request=_RuntimeQueue(
                [
                    '{"outcome":"continue","plan_steps":["step1"],"acceptance_criteria":["ac1"],"risks":[],"demo_requirements":[{"title":"Mobile walkthrough","acceptance_criterion":"ac1","capture_target":"ios","variants":["Invalid input is rejected","Repeat action remains safe"]}],"next_stage":"dev","execution_worker_capability":"macos"}',
                    '{"outcome":"continue","change_summary":["implemented"],"pr_url":"https://example/pull/1","blocker_message":null}',
                    '{"outcome":"continue","guidance":["reran UI proof"],"validation_scope":"current_head_acceptance","feedback":null,"blocker_message":null}',
                ]
            ),
        )
        agents = CodexWorkflowAgents(runtime=runtime)
        request = replace(self._request(), base_branch="main", execution_repo_dir="/tmp/test-repo")
        captured: dict[str, object] = {}

        def _render_prompt(template_name: str, **kwargs) -> str:
            if template_name == "workflow/test_user.j2":
                captured.update(kwargs)
            return template_name

        with (
            patch("orchestrator.core.runtime.agents.render_prompt", side_effect=_render_prompt),
            patch(
                "orchestrator.core.runtime.agents._current_head_diff_paths",
                return_value=["GirlPower/App/OnboardingSlide.swift", "GirlPowerUITests/GirlPowerUITests.swift"],
            ),
        ):
            plan = agents.pm(request, 1, None, [], None, None, None, self._CAPTURE_TARGET_CONSTRAINTS_JSON)
            dev = agents.dev(request, plan, 1, None)
            test_result = agents.test(request, plan, dev, 1)

        self.assertEqual(test_result.validation_scope, "current_head_acceptance")
        self.assertEqual(json.loads(str(captured["acceptance_criteria_json"])), ["ac1"])
        self.assertEqual(json.loads(str(captured["demo_requirements_json"]))[0]["capture_target"], "ios")
        self.assertEqual(
            json.loads(str(captured["current_head_diff_paths_json"])),
            ["GirlPower/App/OnboardingSlide.swift", "GirlPowerUITests/GirlPowerUITests.swift"],
        )
        self.assertEqual(captured["requires_current_head_acceptance_evidence"], "true")

    def test_dev_prompt_receives_current_pr_head_context_for_resumed_remediation(self) -> None:
        runtime = CodexRuntime(
            model="gpt-5-codex",
            max_output_tokens=1200,
            command="codex",
            _request=_RuntimeQueue(
                [
                    '{"outcome":"continue","change_summary":["kept current head intact"],"pr_url":"https://example/pull/9","blocker_message":null}',
                ]
            ),
        )
        agents = CodexWorkflowAgents(runtime=runtime)
        resume_snapshot = ExecutionSnapshot.empty()
        resume_snapshot.apply_stage_checkpoint(
            WorkflowStageCheckpoint(
                stage="review",
                attempt=1,
                status="completed",
                summary="Review found one acceptance bug",
                review_result=ReviewResult(
                    summary=["Fix the demo exit path"],
                    feedback="Keep the existing keychain persistence coverage while remediating the UI issue",
                    pr_url="https://example/pull/9",
                ),
            )
        )
        request = replace(
            self._request(),
            entry_mode="resume",
            entry_stage="dev",
            checkpoint_kind="execution",
            checkpoint_session_id="dev-session-123",
            checkpoint_payload=resume_snapshot.dump(),
            execution_repo_dir="/tmp/test-repo",
            base_branch="main",
        )
        captured: dict[str, object] = {}

        def _render_prompt(template_name: str, **kwargs) -> str:
            if template_name == "workflow/dev_user.j2":
                captured.update(kwargs)
            return template_name

        with (
            patch("orchestrator.core.runtime.agents.render_prompt", side_effect=_render_prompt),
            patch(
                "orchestrator.core.runtime.agents._current_head_diff_paths",
                return_value=[
                    "GirlPower/DemoQuota/KeychainDeviceIdentityStorage.swift",
                    "GirlPowerUITests/GirlPowerUITests.swift",
                ],
            ),
        ):
            dev = agents.dev(
                request,
                PmPlan(plan_steps=["step"], acceptance_criteria=["ac"], risks=[]),
                2,
                "Review failed on demo exit",
            )

        self.assertEqual(dev.pr_url, "https://example/pull/9")
        self.assertEqual(captured["current_pr_url"], "https://example/pull/9")
        self.assertEqual(
            json.loads(str(captured["current_head_diff_paths_json"])),
            [
                "GirlPower/DemoQuota/KeychainDeviceIdentityStorage.swift",
                "GirlPowerUITests/GirlPowerUITests.swift",
            ],
        )

    def test_test_rejects_targeted_only_scope_when_current_head_acceptance_is_required(self) -> None:
        runtime = CodexRuntime(
            model="gpt-5-codex",
            max_output_tokens=1200,
            command="codex",
            _request=_RuntimeQueue(
                [
                    '{"outcome":"continue","plan_steps":["step1"],"acceptance_criteria":["ac1"],"risks":[],"demo_requirements":[{"title":"Mobile walkthrough","acceptance_criterion":"ac1","capture_target":"ios","variants":["Invalid input is rejected","Repeat action remains safe"]}],"next_stage":"dev","execution_worker_capability":"macos"}',
                    '{"outcome":"continue","change_summary":["implemented"],"pr_url":"https://example/pull/1","blocker_message":null}',
                    '{"outcome":"continue","guidance":["unit tests passed"],"validation_scope":"targeted_only","feedback":null,"blocker_message":null}',
                ]
            ),
        )
        agents = CodexWorkflowAgents(runtime=runtime)
        request = replace(self._request(), base_branch="main", execution_repo_dir="/tmp/test-repo")

        with (
            patch("orchestrator.core.runtime.agents.render_prompt", side_effect=lambda template_name, **_: template_name),
            patch(
                "orchestrator.core.runtime.agents._current_head_diff_paths",
                return_value=["GirlPower/App/OnboardingSlide.swift"],
            ),
        ):
            plan = agents.pm(request, 1, None, [], None, None, None, self._CAPTURE_TARGET_CONSTRAINTS_JSON)
            dev = agents.dev(request, plan, 1, None)
            with self.assertRaisesRegex(
                CodexRuntimeError,
                "cannot use targeted_only validation_scope when current-head acceptance evidence is required",
            ):
                agents.test(request, plan, dev, 1)

    def test_stage_prompts_include_answered_human_inputs(self) -> None:
        runtime = CodexRuntime(
            model="gpt-5-codex",
            max_output_tokens=1200,
            command="override",
            _request=_RuntimeQueue(
                [
                    '{"outcome":"continue","plan_steps":["step1"],"acceptance_criteria":["ac1"],"risks":[],"demo_requirements":[{"title":"Demo step1","acceptance_criterion":"ac1","capture_target":"browser","variants":["Invalid input is rejected","Repeat action remains safe"]}],"next_stage":"dev","execution_worker_capability":"linux"}',
                    '{"outcome":"continue","change_summary":["implemented"],"pr_url":null,"blocker_message":null}',
                ]
            ),
        )
        agents = CodexWorkflowAgents(runtime=runtime)
        request = WorkflowRequest(
            tenant_id="tenant-1",
            project_id="project-1",
            project_name="example App",
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

        with patch("orchestrator.core.runtime.agents.render_prompt", side_effect=_render_prompt):
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
                    '{"outcome":"continue","plan_steps":["step1"],"acceptance_criteria":["ac1"],"risks":["risk1"],"demo_requirements":[{"title":"Demo step1","acceptance_criterion":"ac1","capture_target":"browser","variants":["Invalid input is rejected","Repeat action remains safe"]}],"next_stage":"dev","execution_worker_capability":"linux"}',
                    '{"outcome":"continue","change_summary":["implemented"],"pr_url":"https://example/pull/1","blocker_message":null}',
                    '{"outcome":"continue","guidance":["run tests"],"validation_scope":"targeted_only","feedback":null,"blocker_message":null}',
                    '{"outcome":"continue","summary":["looks good"],"feedback":null,"pr_url":"https://example/pull/1","blocker_message":null}',
                ]
            ),
        )
        agents = CodexWorkflowAgents(runtime=runtime)
        request = WorkflowRequest(
            tenant_id="tenant-1",
            project_id="project-1",
            project_name="example App",
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

        with patch("orchestrator.core.runtime.agents.render_prompt", side_effect=_render_prompt):
            plan = agents.pm(request, 1, None, [], None, None, None)
            dev = agents.dev(request, plan, 1, None)
            test_result = agents.test(request, plan, dev, 1)
            agents.review(request, plan, dev, test_result, 1)

        self.assertEqual(captured["allow_pr_creation"], "true")
        self.assertEqual(captured["test_validation_scope"], "targeted_only")
        self.assertEqual(captured["current_head_diff_paths_json"], "[]")
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
                    "demo_requirements": [
                        {
                            "title": "Demo step1",
                            "acceptance_criterion": "ac1",
                            "capture_target": "browser",
                            "variants": ["Invalid input is rejected", "Repeat action remains safe"],
                        }
                    ],
                    "next_stage": "dev",
                    "execution_worker_capability": "linux",
                }
            return {"outcome": "continue", "change_summary": ["implemented"], "pr_url": None}

        with (
            patch("orchestrator.core.runtime.agents.render_prompt", side_effect=lambda template_name, **_: template_name),
            patch("orchestrator.core.runtime.stage_session.invoke_runtime_json_with_tools", side_effect=_invoke_runtime_json),
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
                    pr_url="https://example/pull/9",
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
            patch("orchestrator.core.runtime.agents.render_prompt", side_effect=_render_prompt),
            patch("orchestrator.core.runtime.stage_session.invoke_runtime_json_with_tools", return_value={"outcome": "continue", "summary": ["ok"], "feedback": None, "pr_url": None}),
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
        self.assertEqual(captured["pr_url"], "https://example/pull/9")

    def test_review_resume_reuses_previous_review_pr_url_when_response_omits_it(self) -> None:
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
                stage="review",
                attempt=1,
                status="completed",
                summary="Review already opened PR",
                review_result=ReviewResult(
                    summary=["PR already open"],
                    feedback="Keep existing PR context",
                    pr_url="https://example/pull/9",
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

        with (
            patch("orchestrator.core.runtime.agents.render_prompt", side_effect=lambda template_name, **_: template_name),
            patch(
                "orchestrator.core.runtime.stage_session.invoke_runtime_json_with_tools",
                return_value={"outcome": "continue", "summary": ["ok"], "feedback": None, "pr_url": None},
            ),
        ):
            review = agents.review(
                request,
                PmPlan(plan_steps=["step"], acceptance_criteria=["ac"], risks=[]),
                DevResult(change_summary=["implemented"], pr_url=None),
                TestResult(guidance=["python -m unittest"]),
                1,
            )

        self.assertEqual(review.pr_url, "https://example/pull/9")

    def test_dev_test_and_review_map_structured_blockers(self) -> None:
        runtime = CodexRuntime(
            model="gpt-5-codex",
            max_output_tokens=1200,
            command="override",
            _request=_RuntimeQueue(
                [
                    '{"outcome":"continue","plan_steps":["step1"],"acceptance_criteria":["ac1"],"risks":["risk1"],"demo_requirements":[{"title":"Demo step1","acceptance_criterion":"ac1","capture_target":"browser","variants":["Invalid input is rejected","Repeat action remains safe"]}],"next_stage":"dev","execution_worker_capability":"linux"}',
                    '{"outcome":"blocked","change_summary":["implemented"],"pr_url":null,"blocker_message":"APPLE_TEST_PASSWORD missing"}',
                    '{"outcome":"failed","guidance":["retry targeted UI test"],"validation_scope":"current_head_acceptance","feedback":"UI test failed","blocker_message":"xcodebuild missing"}',
                    '{"outcome":"blocked","summary":["Awaiting approval"],"feedback":"Need PM approval","blocker_message":"Decision owner approval missing"}',
                ]
            ),
        )
        agents = CodexWorkflowAgents(runtime=runtime)
        request = self._request()

        with patch("orchestrator.core.runtime.agents.render_prompt", side_effect=lambda template_name, **_: template_name):
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
                    '{"outcome":"continue","plan_steps":["step1"],"acceptance_criteria":["ac1"],"risks":["risk1"],"demo_requirements":[{"title":"Demo step1","acceptance_criterion":"ac1","capture_target":"browser","variants":["Invalid input is rejected","Repeat action remains safe"]}],"next_stage":"dev","execution_worker_capability":"linux"}',
                    '{"outcome":"continue","change_summary":["implemented"],"pr_url":"https://example/pull/1"}',
                    '{"outcome":"continue","guidance":["run tests"],"validation_scope":"targeted_only","feedback":null}',
                    "approved, do not merge automatically",
                ]
            ),
        )
        agents = CodexWorkflowAgents(runtime=runtime)
        request = self._request()

        with patch("orchestrator.core.runtime.agents.render_prompt", side_effect=lambda template_name, **_: template_name):
            plan = agents.pm(request, 1, None, [], None, None, None)
            dev = agents.dev(request, plan, 1, None)
            test_result = agents.test(request, plan, dev, 1)
            with self.assertRaises(RuntimeJsonContractError):
                agents.review(request, plan, dev, test_result, 1)

    def test_review_outcome_uses_explicit_blocked_contract(self) -> None:
        runtime = CodexRuntime(
            model="gpt-5-codex",
            max_output_tokens=1200,
            command="override",
            _request=_RuntimeQueue(
                [
                    '{"outcome":"continue","plan_steps":["step1"],"acceptance_criteria":["ac1"],"risks":["risk1"],"demo_requirements":[{"title":"Demo step1","acceptance_criterion":"ac1","capture_target":"browser","variants":["Invalid input is rejected","Repeat action remains safe"]}],"next_stage":"dev","execution_worker_capability":"linux"}',
                    '{"outcome":"continue","change_summary":["implemented"],"pr_url":"https://example/pull/1"}',
                    '{"outcome":"continue","guidance":["run tests"],"validation_scope":"targeted_only","feedback":null}',
                    '{"outcome":"blocked","summary":["Governed runtime unavailable"],"feedback":"Governed runtime unavailable","blocker_message":"Governed runtime unavailable"}',
                ]
            ),
        )
        agents = CodexWorkflowAgents(runtime=runtime)
        request = self._request()

        with patch("orchestrator.core.runtime.agents.render_prompt", side_effect=lambda template_name, **_: template_name):
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

        with patch("orchestrator.core.runtime.agents.render_prompt", side_effect=lambda template_name, **_: template_name):
            with self.assertRaises(RuntimeJsonContractError):
                agents.pm(request, 1, None, [], None, None, None)

    def test_answer_board_question(self) -> None:
        runtime = CodexRuntime(
            model="gpt-5-codex",
            max_output_tokens=1200,
            command="override",
            _request=lambda _system, _user, _working_dir=None, _on_log_line=None: '{"message":"2 blocked issues: MAB-1, MAB-2"}',
        )

        with patch("orchestrator.core.runtime.agents.render_prompt", side_effect=lambda template_name, **_: template_name):
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
            patch("orchestrator.core.runtime.agents.render_prompt", side_effect=_render_prompt),
            patch(
                "orchestrator.core.runtime.agents._invoke_discord_json_maybe_tools",
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
            patch("orchestrator.core.runtime.agents.render_prompt", side_effect=_render_prompt),
            patch(
                "orchestrator.core.runtime.agents._invoke_discord_json_maybe_tools",
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

    def test_voice_entry_interview_lane_requires_pm_persona(self) -> None:
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
        with patch("orchestrator.core.runtime.agents.render_prompt", side_effect=lambda template_name, **_: template_name):
            with pytest.raises(CodexRuntimeError, match="interview lane without pm persona"):
                route_voice_entry_with_runtime(
                    runtime=runtime,
                    transcript="We should define MVP scope",
                    entry_source="unit-test",
                    invocation_context=ctx,
                )

    def test_stage_log_sink_emits_payload(self) -> None:
        captured_logs: list[dict] = []

        def _request(_system: str, _user: str, _working_dir: str | None = None, _on_log_line=None) -> str:
            if _on_log_line is not None:
                _on_log_line("stdout", "line-1")
                _on_log_line("stderr", "line-2")
            return '{"outcome":"continue","plan_steps":["step1"],"acceptance_criteria":["ac1"],"risks":[],"demo_requirements":[{"title":"Demo step1","acceptance_criterion":"ac1","capture_target":"browser","variants":["Invalid input is rejected","Repeat action remains safe"]}],"next_stage":"dev","execution_worker_capability":"linux"}'

        runtime = CodexRuntime(
            model="gpt-5-codex",
            max_output_tokens=1200,
            command="override",
            _request=_request,
        )
        agents = CodexWorkflowAgents(runtime=runtime, log_sink=lambda payload: captured_logs.append(payload))

        with patch("orchestrator.core.runtime.agents.render_prompt", side_effect=lambda template_name, **_: template_name):
            plan = agents.pm(self._request(), 1, None, [], None, None, None)
        self.assertEqual(plan.plan_steps, ["step1"])
        self.assertEqual(len(captured_logs), 2)
        self.assertEqual(captured_logs[0]["stage"], "pm")
        self.assertEqual(captured_logs[0]["attempt"], 1)
        self.assertEqual(captured_logs[0]["stream"], "stdout")
        self.assertEqual(captured_logs[0]["message"], "line-1")
        self.assertEqual(captured_logs[1]["stream"], "stderr")

    def test_stage_log_sink_fails_when_log_persistence_fails(self) -> None:
        captured_logs: list[dict] = []

        def _request(_system: str, _user: str, _working_dir: str | None = None, _on_log_line=None) -> str:
            if _on_log_line is not None:
                _on_log_line("stdout", "line-1")
            return '{"outcome":"continue","plan_steps":["step1"],"acceptance_criteria":["ac1"],"risks":[],"demo_requirements":[{"title":"Demo step1","acceptance_criterion":"ac1","capture_target":"browser","variants":["Invalid input is rejected","Repeat action remains safe"]}],"next_stage":"dev","execution_worker_capability":"linux"}'

        runtime = CodexRuntime(
            model="gpt-5-codex",
            max_output_tokens=1200,
            command="override",
            _request=_request,
        )
        agents = CodexWorkflowAgents(runtime=runtime, log_sink=lambda payload: captured_logs.append(payload))

        with (
            patch("orchestrator.core.runtime.agents.render_prompt", side_effect=lambda template_name, **_: template_name),
            patch("orchestrator.core.runtime.invocation._enqueue_runtime_log_line", side_effect=RuntimeError("db down")),
        ):
            with self.assertRaisesRegex(RuntimeError, "db down"):
                agents.pm(self._request(), 1, None, [], None, None, None)
        self.assertEqual(captured_logs, [])

    def test_voice_entry_router_rejects_invalid_lane_and_persona(self) -> None:
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
        with patch("orchestrator.core.runtime.agents.render_prompt", side_effect=lambda template_name, **_: template_name):
            with pytest.raises(CodexRuntimeError, match="invalid lane"):
                route_voice_entry_with_runtime(
                    runtime=runtime,
                    transcript="What is the status?",
                    entry_source="unit-test",
                    invocation_context=ctx,
                )

    def test_voice_entry_router_rejects_legacy_persona_lane(self) -> None:
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
        with patch("orchestrator.core.runtime.agents.render_prompt", side_effect=lambda template_name, **_: template_name):
            with pytest.raises(CodexRuntimeError, match="invalid lane"):
                route_voice_entry_with_runtime(
                    runtime=runtime,
                    transcript="Help me scope this",
                    entry_source="unit-test",
                    invocation_context=ctx,
                )

    def test_voice_entry_router_rejects_legacy_pm_lane(self) -> None:
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
        with patch("orchestrator.core.runtime.agents.render_prompt", side_effect=lambda template_name, **_: template_name):
            with pytest.raises(CodexRuntimeError, match="invalid lane"):
                route_voice_entry_with_runtime(
                    runtime=runtime,
                    transcript="We should build a dashboard",
                    entry_source="unit-test",
                    invocation_context=ctx,
                )

    def test_voice_room_persona_answer_requires_explicit_brief_schema(self) -> None:
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

        with patch("orchestrator.core.runtime.agents.render_prompt", side_effect=lambda template_name, **_: template_name):
            with pytest.raises(CodexRuntimeError, match="Voice room persona payload brief must be an object"):
                answer_voice_room_persona_with_runtime(
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


if __name__ == "__main__":
    unittest.main()
