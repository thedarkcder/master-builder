import unittest

from orchestrator.core.signal_templates import (
    format_discord_pr_ready_message,
    format_jira_final_comment,
)


class SignalTemplateTests(unittest.TestCase):
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
