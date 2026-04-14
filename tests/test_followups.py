import unittest

from orchestrator.core.followups import build_backlog_follow_up_draft


class FollowUpDraftTests(unittest.TestCase):
    def test_follow_up_draft_is_backlog_only_with_required_fields(self) -> None:
        draft = build_backlog_follow_up_draft(
            title="Investigate flaky workflow check parsing",
            why_it_matters="Review stage may block PR-ready signal incorrectly.",
            impact="False negatives delay merges and increase manual triage.",
            suggested_approach="Harden check-suite parsing and add test coverage.",
            origin_issue_key="MAB-1",
            origin_pr_url="https://github.com/example/repo/pull/10",
            additional_labels=("v0.6",),
        )
        payload = draft.to_payload()

        self.assertEqual(payload["target_status"], "Backlog")
        self.assertFalse(payload["auto_promote"])
        self.assertIn("Why it matters", payload["description"])
        self.assertIn("Origin issue: MAB-1", payload["description"])
        self.assertIn("Origin PR: https://github.com/example/repo/pull/10", payload["description"])
        self.assertIn("backlog-only", payload["labels"])
