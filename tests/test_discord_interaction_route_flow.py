from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import select

from orchestrator.api.discord.interactions.parser import (
    _discord_issue_autocomplete_choices,
    _parse_discord_interaction_command,
)
from orchestrator.storage.models import WebhookJob
from tests.test_support.jira_webhook_api_harness import JiraWebhookTestsHarness


pytestmark = pytest.mark.contract


class DiscordInteractionRouteFlowTests(JiraWebhookTestsHarness):
    def test_discord_component_interactions_are_queued_after_immediate_ack(self) -> None:
        payload = {
            "type": 3,
            "application_id": "discord-app-1",
            "token": "interaction-token-1",
            "channel_id": "discord-channel-1",
            "data": {"custom_id": "ask.approve.0123456789abcdef0123456789abcdef"},
            "member": {"user": {"id": "discord-user-1"}},
        }

        with (
            patch("orchestrator.api.routes.webhook_discord_interactions._resolve_discord_interactions_public_key", return_value=b"\x01" * 32),
            patch("orchestrator.api.routes.webhook_discord_interactions._validate_discord_interaction_signature"),
        ):
            response = self.client.post("/discord/interactions", json=payload)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["type"], 5)
        self.assertEqual(body["data"]["flags"], 64)
        with self.session_factory() as session:
            jobs = session.execute(
                select(WebhookJob).where(WebhookJob.transport == "discord_interaction")
            ).scalars().all()
        self.assertEqual(len(jobs), 1)
        self.assertIsNone(jobs[0].dedupe_key)

    def test_discord_issue_autocomplete_passes_channel_id_for_project_scoping(self) -> None:
        payload = {
            "type": 4,
            "channel_id": "discord-channel-1",
            "data": {
                "name": "run",
                "options": [
                    {"type": 3, "name": "issue_key", "value": "TP", "focused": True},
                ],
            },
        }
        fake_tenant = SimpleNamespace(tenant_id="tenant-webhook")

        with (
            patch("orchestrator.api.routes.webhook_discord_interactions._resolve_discord_interactions_public_key", return_value=b"\x01" * 32),
            patch("orchestrator.api.routes.webhook_discord_interactions._validate_discord_interaction_signature"),
            patch("orchestrator.api.discord.interactions.application._find_tenant_for_discord_channel", return_value=fake_tenant),
            patch("orchestrator.api.discord.interactions.application._discord_issue_autocomplete_choices", return_value=[]) as choices_mock,
        ):
            response = self.client.post("/discord/interactions", json=payload)

        self.assertEqual(response.status_code, 200)
        choices_mock.assert_called_once()
        self.assertEqual(choices_mock.call_args.kwargs["channel_id"], "discord-channel-1")

    def test_discord_issue_autocomplete_filters_results_locally(self) -> None:
        fake_tenant = SimpleNamespace(tenant_id="tenant-webhook")
        fake_issues = [
            SimpleNamespace(key="TP-10", summary="Fix auth"),
            SimpleNamespace(key="TP-11", summary="Add notifications"),
            SimpleNamespace(key="ZZ-1", summary="Other project"),
        ]
        with (
            self.session_factory() as session,
            patch("orchestrator.api.discord.interactions.parser._project_filter_jql", return_value='project = "TP"'),
            patch("orchestrator.api.discord.interactions.parser._search_jira_issues_for_tenant", return_value=fake_issues),
        ):
            choices = _discord_issue_autocomplete_choices(
                session=session,
                tenant=fake_tenant,  # type: ignore[arg-type]
                channel_id="discord-channel-1",
                current_value="TP-11",
            )
        self.assertEqual(len(choices), 1)
        self.assertEqual(choices[0]["value"], "TP-11")

    def test_parse_discord_bug_interaction_includes_params_and_attachments(self) -> None:
        payload = {
            "type": 2,
            "channel_id": "discord-channel-1",
            "member": {"user": {"id": "discord-user-1"}},
            "data": {
                "name": "bug",
                "options": [
                    {"type": 3, "name": "summary", "value": "Login fails"},
                    {"type": 3, "name": "details", "value": "Spinner never stops"},
                    {"type": 3, "name": "issue_key", "value": "TP-11"},
                    {"type": 11, "name": "attachment_1", "value": "att-1"},
                ],
                "resolved": {
                    "attachments": {
                        "att-1": {
                            "id": "att-1",
                            "url": "https://cdn.discordapp.com/attachments/att-1.png",
                            "filename": "att-1.png",
                            "content_type": "image/png",
                        }
                    }
                },
            },
        }

        user_id, channel_id, command_text, command_params, attachments = _parse_discord_interaction_command(payload)
        self.assertEqual(user_id, "discord-user-1")
        self.assertEqual(channel_id, "discord-channel-1")
        self.assertEqual(command_text, "!bug Login fails")
        self.assertEqual(command_params["summary"], "Login fails")
        self.assertEqual(command_params["details"], "Spinner never stops")
        self.assertEqual(command_params["issue_key"], "TP-11")
        self.assertEqual(attachments[0]["filename"], "att-1.png")

    def test_parse_discord_gap_interaction_includes_issue_key_param(self) -> None:
        payload = {
            "type": 2,
            "channel_id": "discord-channel-1",
            "member": {"user": {"id": "discord-user-1"}},
            "data": {
                "name": "gap",
                "options": [
                    {"type": 3, "name": "issue_key", "value": "TP-44"},
                ],
            },
        }

        user_id, channel_id, command_text, command_params, attachments = _parse_discord_interaction_command(payload)
        self.assertEqual(user_id, "discord-user-1")
        self.assertEqual(channel_id, "discord-channel-1")
        self.assertEqual(command_text, "!gap TP-44")
        self.assertEqual(command_params, {"issue_key": "TP-44"})
        self.assertEqual(attachments, [])

    def test_discord_reply_button_component_returns_modal(self) -> None:
        payload = {
            "type": 3,
            "application_id": "discord-app-1",
            "token": "interaction-token-1",
            "channel_id": "discord-channel-1",
            "data": {"custom_id": "ask.reply.open"},
            "message": {"id": "123456789012345678"},
            "member": {"user": {"id": "discord-user-1"}},
        }
        fake_tenant = SimpleNamespace(tenant_id="tenant-webhook")

        with (
            patch("orchestrator.api.routes.webhook_discord_interactions._resolve_discord_interactions_public_key", return_value=b"\x01" * 32),
            patch("orchestrator.api.routes.webhook_discord_interactions._validate_discord_interaction_signature"),
            patch("orchestrator.api.discord.interactions.application._find_tenant_for_discord_channel", return_value=fake_tenant),
        ):
            response = self.client.post("/discord/interactions", json=payload)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["type"], 9)
        self.assertEqual(body["data"]["custom_id"], "ask.reply.123456789012345678")
        self.assertEqual(body["data"]["components"][0]["components"][0]["custom_id"], "question")

    def test_discord_reply_message_command_returns_modal(self) -> None:
        payload = {
            "type": 2,
            "application_id": "discord-app-1",
            "token": "interaction-token-1",
            "channel_id": "discord-channel-1",
            "data": {
                "name": "reply",
                "type": 3,
                "target_id": "123456789012345678",
                "resolved": {
                    "messages": {
                        "123456789012345678": {
                            "id": "123456789012345678",
                            "author": {"id": "discord-app-1"},
                        }
                    }
                },
            },
            "member": {"user": {"id": "discord-user-1"}},
        }

        with (
            patch("orchestrator.api.routes.webhook_discord_interactions._resolve_discord_interactions_public_key", return_value=b"\x01" * 32),
            patch("orchestrator.api.routes.webhook_discord_interactions._validate_discord_interaction_signature"),
        ):
            response = self.client.post("/discord/interactions", json=payload)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["type"], 9)
        self.assertEqual(body["data"]["custom_id"], "ask.reply.123456789012345678")
        self.assertEqual(body["data"]["components"][0]["components"][0]["custom_id"], "question")

    def test_discord_reply_modal_submit_is_queued_after_immediate_ack(self) -> None:
        payload = {
            "type": 5,
            "application_id": "discord-app-1",
            "token": "interaction-token-1",
            "channel_id": "discord-channel-1",
            "data": {
                "custom_id": "ask.reply.123456789012345678",
                "components": [
                    {
                        "type": 1,
                        "components": [
                            {
                                "type": 4,
                                "custom_id": "question",
                                "value": "What changed since the previous update?",
                            }
                        ],
                    }
                ],
            },
            "member": {"user": {"id": "discord-user-1"}},
        }

        with (
            patch("orchestrator.api.routes.webhook_discord_interactions._resolve_discord_interactions_public_key", return_value=b"\x01" * 32),
            patch("orchestrator.api.routes.webhook_discord_interactions._validate_discord_interaction_signature"),
        ):
            response = self.client.post("/discord/interactions", json=payload)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["type"], 5)
        self.assertEqual(body["data"]["flags"], 64)
        with self.session_factory() as session:
            jobs = session.execute(
                select(WebhookJob).where(WebhookJob.transport == "discord_interaction")
            ).scalars().all()
        self.assertEqual(len(jobs), 1)
        self.assertIsNone(jobs[0].dedupe_key)
