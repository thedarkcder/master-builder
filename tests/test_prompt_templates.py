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


if __name__ == "__main__":
    unittest.main()
