from __future__ import annotations

import unittest
from unittest.mock import patch
from pathlib import Path

from orchestrator.core.prompt_templates import render_prompt


class PromptTemplateTests(unittest.TestCase):
    def test_render_prompt_fails_fast_when_jinja_missing(self) -> None:
        with patch("orchestrator.core.prompt_templates._jinja_environment", side_effect=RuntimeError("jinja2 missing")):
            with self.assertRaisesRegex(RuntimeError, "jinja2 missing"):
                render_prompt("workflow/pm_user.j2")

    def test_render_prompt_uses_template_environment(self) -> None:
        class _Template:
            def render(self, **context):  # noqa: ANN003
                return f"Tenant={context['tenant_id']}"

        class _Env:
            def get_template(self, template_name: str):  # noqa: ANN001
                self.template_name = template_name
                return _Template()

        fake_env = _Env()
        with patch("orchestrator.core.prompt_templates._jinja_environment", return_value=fake_env):
            rendered = render_prompt("workflow/pm_user.j2", tenant_id="tenant-1")
        self.assertEqual(fake_env.template_name, "workflow/pm_user.j2")
        self.assertEqual(rendered, "Tenant=tenant-1")

    def test_pm_user_prompt_enforces_macos_signals_for_ios_work(self) -> None:
        prompt_path = (
            Path(__file__).resolve().parents[1]
            / "orchestrator"
            / "prompts"
            / "workflow"
            / "pm_user.j2"
        )
        prompt_text = prompt_path.read_text(encoding="utf-8")
        self.assertIn("mandatory macos signals", prompt_text)
        self.assertIn("xcodebuild", prompt_text)
        self.assertIn("XCUITest", prompt_text)
        self.assertIn('Never output "linux" when mandatory macos signals exist', prompt_text)

    def test_pm_user_prompt_defines_decision_state_evidence_contract(self) -> None:
        prompt_path = (
            Path(__file__).resolve().parents[1]
            / "orchestrator"
            / "prompts"
            / "workflow"
            / "pm_user.j2"
        )
        prompt_text = prompt_path.read_text(encoding="utf-8")
        self.assertIn("`decision_state` means the persisted Decision Gate / clarification state", prompt_text)
        self.assertIn("The only authoritative way to determine `decision_state` is the `decision.read_state` tool", prompt_text)
        self.assertIn('Do not emit `missing_evidence_sources=["decision_state"]` unless you actually called `decision.read_state`', prompt_text)
        self.assertIn("If `decision.read_state` succeeds and reports that no prior decision state exists, that is a valid result", prompt_text)

    def test_pm_user_prompt_defines_run_request_human_input_contract(self) -> None:
        prompt_path = (
            Path(__file__).resolve().parents[1]
            / "orchestrator"
            / "prompts"
            / "workflow"
            / "pm_user.j2"
        )
        prompt_text = prompt_path.read_text(encoding="utf-8")
        self.assertIn("`run.request_human_input` requires this exact argument shape", prompt_text)
        self.assertIn('"request_type":"<stable snake_case type>"', prompt_text)
        self.assertIn("Never emit top-level `questions`", prompt_text)
        self.assertIn('use `request_type="decision_gate_clarification"`', prompt_text)

    def test_test_user_prompt_requires_changed_scope_before_full_suite(self) -> None:
        prompt_path = (
            Path(__file__).resolve().parents[1]
            / "orchestrator"
            / "prompts"
            / "workflow"
            / "test_user.j2"
        )
        prompt_text = prompt_path.read_text(encoding="utf-8")
        self.assertIn("execute targeted tests/checks only for the code you changed first", prompt_text)
        self.assertIn("Do not default to broad `xcodebuild test`", prompt_text)
        self.assertIn("If you cannot identify a targeted test", prompt_text)
        self.assertIn("inspect the allowed tool list", prompt_text)
        self.assertIn("before concluding the run is blocked", prompt_text)

    def test_dev_and_review_prompts_require_pr_head_to_use_integration_branch(self) -> None:
        prompts_dir = Path(__file__).resolve().parents[1] / "orchestrator" / "prompts" / "workflow"
        dev_prompt_text = (prompts_dir / "dev_user.j2").read_text(encoding="utf-8")
        review_prompt_text = (prompts_dir / "review_user.j2").read_text(encoding="utf-8")

        self.assertIn("Integration branch as the only valid PR head branch", dev_prompt_text)
        self.assertIn("Never open or update a PR from the Execution branch", dev_prompt_text)
        self.assertIn("inspect the allowed tool list", dev_prompt_text)
        self.assertIn("Integration branch as the canonical PR head branch", review_prompt_text)
        self.assertIn("Execution branch (`run/...`)", review_prompt_text)
        self.assertIn("inspect the allowed tool list", review_prompt_text)

    def test_workflow_stage_prompts_treat_tool_base_as_part_of_diagnosis(self) -> None:
        prompts_dir = Path(__file__).resolve().parents[1] / "orchestrator" / "prompts" / "workflow"
        for prompt_name in ("pm_user.j2", "dev_system.j2", "test_system.j2", "review_system.j2"):
            prompt_text = (prompts_dir / prompt_name).read_text(encoding="utf-8")
            self.assertIn("inspect the allowed tool list", prompt_text)
            self.assertIn("before concluding the blocker is real", prompt_text)

    def test_execution_stage_system_prompts_frame_tools_as_diagnostic_catalog(self) -> None:
        prompts_dir = Path(__file__).resolve().parents[1] / "orchestrator" / "prompts" / "workflow"
        for prompt_name in ("dev_system.j2", "test_system.j2", "review_system.j2"):
            prompt_text = (prompts_dir / prompt_name).read_text(encoding="utf-8")
            self.assertIn("authoritative path for governed context and diagnosis", prompt_text)
            self.assertNotIn("governed side effects", prompt_text)

    def test_voice_room_engineer_prompt_enforces_spoken_style(self) -> None:
        system_prompt_path = (
            Path(__file__).resolve().parents[1]
            / "orchestrator"
            / "prompts"
            / "discord"
            / "voice_room_engineer_system.j2"
        )
        user_prompt_path = (
            Path(__file__).resolve().parents[1]
            / "orchestrator"
            / "prompts"
            / "discord"
            / "voice_room_engineer_user.j2"
        )
        system_prompt_text = system_prompt_path.read_text(encoding="utf-8")
        user_prompt_text = user_prompt_path.read_text(encoding="utf-8")

        self.assertIn("answering someone out loud", system_prompt_text)
        self.assertIn("Avoid phrases like \"Engineering-wise\"", system_prompt_text)
        self.assertIn("Use clean, grammatical sentences with one main idea per sentence.", system_prompt_text)
        self.assertIn("Avoid opening with filler like \"Today\", \"Right now\", or \"Currently\"", system_prompt_text)
        self.assertIn("Write for speech, not for a sprint update.", user_prompt_text)
        self.assertIn("Include ticket numbers when they materially anchor the answer.", user_prompt_text)
        self.assertIn("Mention them naturally in spoken language", user_prompt_text)
        self.assertIn("Use clean, grammatical sentences with one main idea per sentence.", user_prompt_text)

    def test_voice_room_persona_prompts_treat_transcript_as_direct_request(self) -> None:
        prompts_dir = Path(__file__).resolve().parents[1] / "orchestrator" / "prompts" / "discord"
        for persona in ("architect", "engineer", "pm", "qa", "security"):
            system_prompt_text = (prompts_dir / f"voice_room_{persona}_system.j2").read_text(encoding="utf-8")
            user_prompt_text = (prompts_dir / f"voice_room_{persona}_user.j2").read_text(encoding="utf-8")

            self.assertIn("Treat the routed transcript as the user's direct request to you.", system_prompt_text)
            self.assertIn("direct", system_prompt_text)
            self.assertIn("Treat the transcript as the user's direct request", user_prompt_text)
            self.assertIn("Prefer the best direct", user_prompt_text)

    def test_voice_room_pm_prompt_avoids_scope_triage_fallback(self) -> None:
        prompts_dir = Path(__file__).resolve().parents[1] / "orchestrator" / "prompts" / "discord"
        system_prompt_text = (prompts_dir / "voice_room_pm_system.j2").read_text(encoding="utf-8")
        user_prompt_text = (prompts_dir / "voice_room_pm_user.j2").read_text(encoding="utf-8")

        self.assertIn("Do not default to intake or scope-triage language", system_prompt_text)
        self.assertIn("Prefer the best direct recommendation, decision, or next step", user_prompt_text)
        self.assertIn("Do not fall back to intake or scope-triage language", user_prompt_text)
        self.assertNotIn("If the brief is incomplete", user_prompt_text)

    def test_pm_persona_prompts_encode_customer_vision_and_outcome_ownership(self) -> None:
        discord_prompts_dir = Path(__file__).resolve().parents[1] / "orchestrator" / "prompts" / "discord"
        workflow_prompts_dir = Path(__file__).resolve().parents[1] / "orchestrator" / "prompts" / "workflow"

        pm_answer_system = (discord_prompts_dir / "pm_answer_system.j2").read_text(encoding="utf-8")
        voice_room_pm_system = (discord_prompts_dir / "voice_room_pm_system.j2").read_text(encoding="utf-8")
        workflow_pm_system = (workflow_prompts_dir / "pm_system.j2").read_text(encoding="utf-8")

        for prompt_text in (pm_answer_system, voice_room_pm_system):
            self.assertIn("Listen deeply to customers and stakeholders", prompt_text)
            self.assertIn("Hold a clear product vision", prompt_text)
            self.assertIn("Be decisive but open to evidence and feedback", prompt_text)
            self.assertIn("Bridge engineering, design, and go-to-market teams", prompt_text)
            self.assertIn("Own outcomes and value delivery", prompt_text)

        self.assertIn("Ground planning decisions in user value and business outcomes", workflow_pm_system)

    def test_decision_planner_prompts_forbid_direct_db_inspection(self) -> None:
        prompts_dir = Path(__file__).resolve().parents[1] / "orchestrator" / "prompts" / "policy"
        system_prompt_text = (prompts_dir / "decision_planner_system.j2").read_text(encoding="utf-8")
        user_prompt_text = (prompts_dir / "decision_planner_user.j2").read_text(encoding="utf-8")

        self.assertIn("Never improvise direct database inspection", system_prompt_text)
        self.assertIn("Persisted decision state must be read via `decision.read_state`", user_prompt_text)
        self.assertIn("If `decision.read_state` or another allowed tool fails", user_prompt_text)
        self.assertIn('"type":"tool_request"', user_prompt_text)
        self.assertIn('"type":"final_response"', user_prompt_text)
        self.assertNotIn("Agent tool command:", user_prompt_text)

    def test_workflow_stage_prompts_forbid_direct_db_inspection(self) -> None:
        prompts_dir = Path(__file__).resolve().parents[1] / "orchestrator" / "prompts" / "workflow"
        expected_text = (
            "Do not inspect application or planner state directly with Python, shell, SQL, or raw database clients;"
        )
        for prompt_name in ("pm_system.j2", "dev_system.j2", "test_system.j2", "review_system.j2"):
            prompt_text = (prompts_dir / prompt_name).read_text(encoding="utf-8")
            self.assertIn(expected_text, prompt_text)

        for prompt_name in ("pm_user.j2", "dev_user.j2", "test_user.j2", "review_user.j2"):
            prompt_text = (prompts_dir / prompt_name).read_text(encoding="utf-8")
            self.assertIn('"type":"tool_request"', prompt_text)
            self.assertIn('"type":"final_response"', prompt_text)
            self.assertNotIn("Agent tool command:", prompt_text)


if __name__ == "__main__":
    unittest.main()
