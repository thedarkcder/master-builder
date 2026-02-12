from __future__ import annotations

import re
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from orchestrator.api.discord.shared.followup_format import (
    build_ask_confirmation_components,
    build_command_followup_message,
    resolve_tenant_jira_browse_base_url,
)


class DiscordFollowupFormatTests(unittest.TestCase):
    def _response(self, *, command: str, message: str = "", data: dict | None = None) -> SimpleNamespace:
        return SimpleNamespace(command=command, message=message, data=data or {})

    def test_build_ask_confirmation_components(self) -> None:
        components = build_ask_confirmation_components("req-123")
        self.assertEqual(len(components), 1)
        buttons = components[0]["components"]
        self.assertEqual(buttons[0]["custom_id"], "ask.approve.req-123")
        self.assertEqual(buttons[1]["custom_id"], "ask.reject.req-123")

    def test_bug_and_issues_messages_include_created_issue_links(self) -> None:
        pattern = re.compile(r"\b[A-Z][A-Z0-9_]+-\d+\b")
        response = self._response(command="issues", data={"created_issue_keys": [" mab-1 ", "MAB-2"]})

        message = build_command_followup_message(
            user_id="u1",
            command_response=response,
            jira_browse_base_url="https://jira.example.com",
            issue_key_pattern=pattern,
        )

        self.assertIn("<@u1> Issue seeding completed.", message)
        self.assertIn("[MAB-1](https://jira.example.com/browse/MAB-1)", message)
        self.assertIn("[MAB-2](https://jira.example.com/browse/MAB-2)", message)

    def test_link_command_formats_links_section(self) -> None:
        pattern = re.compile(r"\b[A-Z][A-Z0-9_]+-\d+\b")
        response = self._response(
            command="link",
            data={
                "issue_key": "mab-42",
                "jira_url": "https://jira.example.com/browse/MAB-42",
                "pr_url": "https://github.com/org/repo/pull/42",
            },
        )

        message = build_command_followup_message(
            user_id="u2",
            command_response=response,
            jira_browse_base_url="https://jira.example.com",
            issue_key_pattern=pattern,
        )

        self.assertIn("Here are the links.", message)
        self.assertIn("Links:", message)
        self.assertIn("- Jira: [MAB-42](https://jira.example.com/browse/MAB-42)", message)
        self.assertIn("- PR: [Open PR](https://github.com/org/repo/pull/42)", message)

    def test_run_and_retry_commands_include_queued_block(self) -> None:
        pattern = re.compile(r"\b[A-Z][A-Z0-9_]+-\d+\b")
        response = self._response(command="run", data={"issue_key": "mab-50", "run_id": "run-123"})

        message = build_command_followup_message(
            user_id="u3",
            command_response=response,
            jira_browse_base_url=None,
            issue_key_pattern=pattern,
        )

        self.assertIn("<@u3> Run queued.", message)
        self.assertIn("Queued:", message)
        self.assertIn("- Issue: MAB-50", message)
        self.assertIn("- Run ID: `run-123`", message)

    def test_runs_and_status_commands_include_entries(self) -> None:
        pattern = re.compile(r"\b[A-Z][A-Z0-9_]+-\d+\b")
        runs_response = self._response(
            command="runs",
            data={
                "runs": [
                    {
                        "run_id": "run-1",
                        "issue_key": "MAB-1",
                        "status": "queued",
                        "pr_url": "https://github.com/org/repo/pull/1",
                    },
                    "invalid",
                ]
            },
        )
        runs_message = build_command_followup_message(
            user_id="u4",
            command_response=runs_response,
            jira_browse_base_url="https://jira.example.com",
            issue_key_pattern=pattern,
        )
        self.assertIn("Recent runs:", runs_message)
        self.assertIn("`run-1` | queued | [MAB-1](https://jira.example.com/browse/MAB-1) | [PR]", runs_message)

        status_response = self._response(
            command="status",
            data={"active_runs": [{"run_id": "run-2", "issue_key": "MAB-2", "status": "running"}]},
        )
        status_message = build_command_followup_message(
            user_id="u4",
            command_response=status_response,
            jira_browse_base_url="https://jira.example.com",
            issue_key_pattern=pattern,
        )
        self.assertIn("Active runs:", status_message)
        self.assertIn("`run-2` | running | [MAB-2](https://jira.example.com/browse/MAB-2)", status_message)

    def test_ask_and_gap_linkify_issue_mentions(self) -> None:
        pattern = re.compile(r"\b[A-Z][A-Z0-9_]+-\d+\b")
        response = self._response(command="ask", message="Check MAB-21 and MAB-22")

        message = build_command_followup_message(
            user_id="u5",
            command_response=response,
            jira_browse_base_url="https://jira.example.com",
            issue_key_pattern=pattern,
        )

        self.assertIn("[MAB-21](https://jira.example.com/browse/MAB-21)", message)
        self.assertIn("[MAB-22](https://jira.example.com/browse/MAB-22)", message)

    def test_content_truncation_and_created_issue_overflow(self) -> None:
        pattern = re.compile(r"\b[A-Z][A-Z0-9_]+-\d+\b")
        keys = [f"MAB-{idx}" for idx in range(1, 26)]
        response = self._response(command="bug", data={"created_issue_keys": keys}, message="x" * 3000)

        message = build_command_followup_message(
            user_id="u6",
            command_response=response,
            jira_browse_base_url=None,
            issue_key_pattern=pattern,
        )

        self.assertIn("...and 5 more", message)
        self.assertLessEqual(len(message), 1900)

    def test_resolve_tenant_jira_browse_base_url(self) -> None:
        tenant = SimpleNamespace(jira_config={})
        session = MagicMock()
        self.assertIsNone(resolve_tenant_jira_browse_base_url(session=session, tenant=tenant))

        tenant = SimpleNamespace(jira_config={"connection_id": "conn-1"})
        session.get.return_value = None
        self.assertIsNone(resolve_tenant_jira_browse_base_url(session=session, tenant=tenant))

        session.get.return_value = SimpleNamespace(site_url="")
        self.assertIsNone(resolve_tenant_jira_browse_base_url(session=session, tenant=tenant))

        session.get.return_value = SimpleNamespace(site_url="https://jira.example.com/")
        self.assertEqual(
            resolve_tenant_jira_browse_base_url(session=session, tenant=tenant),
            "https://jira.example.com",
        )


if __name__ == "__main__":
    unittest.main()
