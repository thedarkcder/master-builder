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


if __name__ == "__main__":
    unittest.main()
