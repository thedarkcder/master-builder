import unittest

from orchestrator.core.signal_templates import (
    format_discord_ready_gate_guidance,
    format_stage_discord_update,
    format_stage_jira_update,
    format_discord_pr_ready_message,
    format_jira_final_comment,
)


class SignalTemplateTests(unittest.TestCase):
    def test_ready_gate_guidance_is_actionable(self) -> None:
        guidance = format_discord_ready_gate_guidance(
            issue_key="MAB-20",
            issue_status="To Do",
            ready_statuses=("Ready for Agent", "Ready"),
        )
        self.assertIn("MAB-20", guidance)
        self.assertIn("To Do", guidance)
        self.assertIn("Ready for Agent", guidance)
        self.assertIn("Move the issue to a ready status", guidance)

    def test_stage_update_templates_include_core_identifiers(self) -> None:
        discord_message = format_stage_discord_update(
            tenant_id="tenant-demo",
            issue_key="MAB-17",
            run_id="run-123",
            stage="pr_opened",
            jira_url="https://example.atlassian.net/browse/MAB-17",
            pr_url="https://github.com/example/repo/pull/5",
        )
        jira_message = format_stage_jira_update(
            tenant_id="tenant-demo",
            issue_key="MAB-17",
            run_id="run-123",
            stage="run_failed",
            jira_url="https://example.atlassian.net/browse/MAB-17",
            error="test stage failed",
            next_steps=("Investigate CI logs", "Rerun once fixed"),
        )

        self.assertIn("tenant-demo", discord_message)
        self.assertIn("MAB-17", discord_message)
        self.assertIn("run-123", discord_message)
        self.assertIn("pr_opened", discord_message)
        self.assertIn("run_failed", jira_message)
        self.assertIn("test stage failed", jira_message)
        self.assertIn("Investigate CI logs", jira_message)

    def test_discord_template_applies_signal_limits(self) -> None:
        message = format_discord_pr_ready_message(
            pr_url="https://github.com/example/repo/pull/1",
            jira_url="https://example.atlassian.net/browse/MAB-1",
            run_id="run-123",
            what_changed=("a", "b", "c", "d"),
            risk_impact=("r1", "r2", "r3"),
            how_to_test=("t1", "t2", "t3", "t4"),
            questions=("q1", "q2", "q3"),
            next_action="Please review + merge",
        )

        self.assertIn("✅ PR Ready", message)
        self.assertIn("Jira:", message)
        self.assertLessEqual(message.count("\n- "), 8)  # changed(3) + risk(2) + q(2) + next action(1)
        self.assertIn("1) t1", message)
        self.assertIn("3) t3", message)
        self.assertNotIn("4) t4", message)

    def test_jira_comment_template_contains_required_sections(self) -> None:
        comment = format_jira_final_comment(
            pr_url="https://github.com/example/repo/pull/2",
            summary=("added policy wiring", "added follow-up draft support"),
            acceptance_criteria=("signals are concise", "follow-ups remain backlog"),
            command_steps=("python3 -m unittest",),
            manual_steps=("Open PR and verify CI + Security are green.",),
            notes=("No migration required.",),
            open_questions=("None",),
            follow_up_issues=("MAB-500: improve reviewer diagnostics",),
        )

        self.assertIn("PR: https://github.com/example/repo/pull/2", comment)
        self.assertIn("Summary:", comment)
        self.assertIn("Acceptance criteria:", comment)
        self.assertIn("How to test:", comment)
        self.assertIn("Follow-ups created (Backlog):", comment)
