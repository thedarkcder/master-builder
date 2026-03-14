from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from orchestrator.api.discord.interactions.parser import (
    _discord_issue_autocomplete_choices,
    _discord_option_attachment_ids,
    _discord_option_value,
    _discord_resolved_attachments,
    _find_focused_discord_option,
    _flatten_discord_option_values,
    _parse_discord_interaction_command,
)


class DiscordInteractionsParserTests(unittest.TestCase):
    def test_flatten_and_focus_and_option_value_helpers(self) -> None:
        options = [
            {
                "type": 1,
                "name": "sub",
                "options": [
                    {"type": 3, "name": "alpha", "value": "A"},
                    {"type": 3, "name": "focused", "value": "B", "focused": True},
                ],
            },
            {"type": 3, "name": "root", "value": "C"},
        ]
        self.assertEqual(_flatten_discord_option_values(options), ["sub", "A", "B", "C"])
        self.assertEqual(_find_focused_discord_option(options), ("focused", "B"))
        self.assertEqual(_discord_option_value(options, name="alpha"), "A")
        self.assertIsNone(_discord_option_value(options, name="missing"))

    def test_attachment_helpers(self) -> None:
        options = [
            {"type": 11, "name": "file1", "value": "a1"},
            {"type": 1, "name": "sub", "options": [{"type": 11, "name": "file2", "value": "a2"}]},
            {"type": 11, "name": "empty", "value": ""},
        ]
        self.assertEqual(_discord_option_attachment_ids(options), ["a1", "a2"])

        data = {
            "resolved": {
                "attachments": {
                    "a1": {
                        "id": "a1",
                        "url": "https://x/1",
                        "proxy_url": "https://media.discordapp.net/x/1",
                        "filename": "1.png",
                    },
                    "a2": {"id": "a2", "url": "https://x/2", "filename": "2.png"},
                    "a3": {"id": "a3", "filename": "3.png"},
                }
            }
        }
        normalized = _discord_resolved_attachments(data=data, attachment_ids=["a1", "a2", "a3"])
        self.assertEqual([item["id"] for item in normalized], ["a1", "a2"])
        self.assertEqual(normalized[0]["proxy_url"], "https://media.discordapp.net/x/1")

    def test_issue_autocomplete_filters_dedupes_and_limits(self) -> None:
        issues = [SimpleNamespace(key="MAB-1", summary="first"), SimpleNamespace(key="MAB-1", summary="dup")]
        issues.extend(SimpleNamespace(key=f"MAB-{i}", summary=f"issue {i}") for i in range(2, 35))
        tenant = SimpleNamespace()
        with (
            patch("orchestrator.api.discord.interactions.parser._project_filter_jql", return_value='project = "MAB"'),
            patch("orchestrator.api.discord.interactions.parser._search_jira_issues_for_tenant", return_value=issues),
        ):
            choices = _discord_issue_autocomplete_choices(
                session=MagicMock(),
                tenant=tenant,
                channel_id="c-1",
                current_value="mab",
            )
        self.assertLessEqual(len(choices), 25)
        self.assertTrue(all(choice["value"].startswith("MAB-") for choice in choices))

    def test_parse_interaction_validates_required_fields(self) -> None:
        with self.assertRaises(HTTPException):
            _parse_discord_interaction_command({})
        with self.assertRaises(HTTPException):
            _parse_discord_interaction_command({"data": {}, "channel_id": "c1"})
        with self.assertRaises(HTTPException):
            _parse_discord_interaction_command({"data": {"name": "ask"}, "channel_id": " "})
        with self.assertRaises(HTTPException):
            _parse_discord_interaction_command({"data": {"name": "ask"}, "channel_id": "c1"})

    def test_parse_ask_run_retry_and_request_commands(self) -> None:
        payload_ask = {
            "data": {
                "name": "ask",
                "options": [
                    {"type": 3, "name": "issue_key", "value": "MAB-1"},
                    {"type": 3, "name": "question", "value": "what changed?"},
                ],
            },
            "channel_id": "c1",
            "user": {"id": "u1"},
        }
        user_id, channel_id, command_text, command_params, attachments = _parse_discord_interaction_command(payload_ask)
        self.assertEqual((user_id, channel_id), ("u1", "c1"))
        self.assertEqual(command_text, "!ask @MAB-1 what changed?")
        self.assertIsNone(command_params)
        self.assertEqual(attachments, [])

        payload_pm = {
            "data": {
                "name": "pm",
                "options": [
                    {"type": 3, "name": "question", "value": "what should we ship first?"},
                ],
            },
            "channel_id": "c1",
            "user": {"id": "u1"},
        }
        parsed_pm = _parse_discord_interaction_command(payload_pm)
        self.assertEqual(parsed_pm[2], "!pm what should we ship first?")

        payload_pm_approve = {
            "data": {
                "name": "pm",
                "options": [
                    {"type": 3, "name": "action", "value": "approve"},
                    {"type": 3, "name": "question", "value": "approve rollout to beta?"},
                ],
            },
            "channel_id": "c1",
            "user": {"id": "u1"},
        }
        parsed_pm_approve = _parse_discord_interaction_command(payload_pm_approve)
        self.assertEqual(parsed_pm_approve[2], "!pm approve approve rollout to beta?")

        payload_run = {
            "data": {"name": "run", "options": [{"type": 3, "name": "issue_key", "value": "MAB-2"}]},
            "channel_id": "c1",
            "member": {"user": {"id": "u2"}},
        }
        parsed_run = _parse_discord_interaction_command(payload_run)
        self.assertEqual(parsed_run[2], "!run MAB-2")

        payload_retry = {
            "data": {"name": "retry", "options": [{"type": 3, "name": "target", "value": "latest"}]},
            "channel_id": "c1",
            "user": {"id": "u2"},
        }
        parsed_retry = _parse_discord_interaction_command(payload_retry)
        self.assertEqual(parsed_retry[2], "!retry latest")

        payload_request = {
            "data": {
                "name": "request",
                "options": [
                    {"type": 3, "name": "permission", "value": "write"},
                    {"type": 3, "name": "reason", "value": "need release"},
                ],
            },
            "channel_id": "c1",
            "user": {"id": "u2"},
        }
        parsed_request = _parse_discord_interaction_command(payload_request)
        self.assertEqual(parsed_request[2], "!request write need release")

    def test_parse_issues_and_bug_commands(self) -> None:
        payload_issues = {
            "data": {
                "name": "issues",
                "options": [
                    {
                        "type": 1,
                        "name": "seed",
                        "options": [{"type": 3, "name": "spec", "value": "create stories"}],
                    }
                ],
            },
            "channel_id": "c1",
            "user": {"id": "u3"},
        }
        parsed_issues = _parse_discord_interaction_command(payload_issues)
        self.assertEqual(parsed_issues[2], "!issues seed create stories")

        payload_bug = {
            "data": {
                "name": "bug",
                "options": [
                    {"type": 3, "name": "summary", "value": "bad state"},
                    {"type": 3, "name": "details", "value": "steps..."},
                    {"type": 3, "name": "issue_key", "value": "MAB-3"},
                    {"type": 11, "name": "attachment", "value": "att-1"},
                ],
                "resolved": {"attachments": {"att-1": {"id": "att-1", "url": "https://x", "filename": "bug.png"}}},
            },
            "channel_id": "c1",
            "user": {"id": "u3"},
        }
        user_id, channel_id, command_text, params, attachments = _parse_discord_interaction_command(payload_bug)
        self.assertEqual((user_id, channel_id), ("u3", "c1"))
        self.assertEqual(command_text, "!bug bad state")
        self.assertEqual(params["issue_key"], "MAB-3")
        self.assertEqual(params["details"], "steps...")
        self.assertEqual(attachments[0]["id"], "att-1")

    def test_parse_fallback_command(self) -> None:
        payload = {
            "data": {"name": "custom", "options": [{"type": 3, "name": "arg", "value": "value"}]},
            "channel_id": "c1",
            "user": {"id": "u4"},
        }
        parsed = _parse_discord_interaction_command(payload)
        self.assertEqual(parsed[2], "!custom value")


if __name__ == "__main__":
    unittest.main()
