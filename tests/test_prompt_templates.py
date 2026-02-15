from __future__ import annotations

import unittest

from orchestrator.core.prompt_templates import render_prompt


class PromptTemplateTests(unittest.TestCase):
    def test_render_pm_user_template(self) -> None:
        rendered = render_prompt(
            "workflow/pm_user.j2",
            tenant_id="tenant-1",
            run_id="run-1",
            issue_key="TP-1",
            issue_summary="summary",
            issue_description="description",
        )
        self.assertIn("Stage: pm", rendered)
        self.assertIn("Tenant ID: tenant-1", rendered)
        self.assertIn("Issue key: TP-1", rendered)

    def test_render_dev_system_template_contains_required_action(self) -> None:
        rendered = render_prompt("workflow/dev_system.j2")
        self.assertIn("must implement code changes", rendered.lower())
        self.assertIn("empty repository", rendered.lower())
        self.assertIn("valid greenfield starting point", rendered.lower())
        self.assertIn("only report blocked:", rendered.lower())
        self.assertIn("return strict json", rendered.lower())


if __name__ == "__main__":
    unittest.main()
