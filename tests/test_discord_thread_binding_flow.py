from __future__ import annotations

import asyncio
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from orchestrator.api.discord.interactions.followup import _run_discord_command_followup
from orchestrator.core.discord.gateway_listener import DiscordGatewayListener

pytestmark = pytest.mark.contract


class DiscordThreadBindingFlowTests(unittest.TestCase):
    def test_failed_run_followup_thread_reply_routes_as_issue_bound_reply(self) -> None:
        tenant = SimpleNamespace(tenant_id="tenant-1", is_enabled=True, jira_config={}, discord_config={}, updated_at=None)
        project = SimpleNamespace(discord_config={"channel_id": "channel-1"}, updated_at=None)
        session = MagicMock()
        session.get.return_value = tenant
        session.execute.return_value.scalars.return_value.all.return_value = [project]

        settings = SimpleNamespace(
            discord_bot_token_secret_ref="token/ref",
            secrets_encryption_key="enc",
        )

        with (
            patch("orchestrator.api.discord.interactions.followup.create_session_factory", return_value=lambda: nullcontext(session)),
            patch("orchestrator.api.discord.interactions.followup.get_settings", return_value=settings),
            patch(
                "orchestrator.api.discord.interactions.followup.execute_discord_ingress_command",
                side_effect=HTTPException(
                    status_code=409,
                    detail=(
                        "Decision Gate still needs clarification for `GP-114`.\n"
                        "Reason: Dependencies and risks are identified."
                    ),
                ),
            ),
            patch("orchestrator.api.discord.interactions.followup.resolve_platform_secret_ref", return_value="token"),
            patch("orchestrator.api.discord.interactions.followup_runtime.resolve_platform_secret_ref", return_value="token"),
            patch("orchestrator.api.discord.interactions.followup._resolve_project_for_channel", return_value=project),
            patch("orchestrator.api.discord.interactions.followup_threading.upsert_followup_context") as upsert_context_mock,
            patch("orchestrator.api.discord.interactions.followup_transport.DiscordApiClient") as followup_client_cls,
            patch("orchestrator.api.discord.interactions.followup._send_discord_interaction_followup") as interaction_followup_mock,
        ):
            followup_client = MagicMock()
            followup_client.post_message.side_effect = [{"id": "run-msg-1"}, {"id": "thread-intro-msg"}]
            followup_client.create_thread_from_message.return_value = "thread-gp114"
            followup_client_cls.return_value = followup_client

            asyncio.run(
                _run_discord_command_followup(
                    tenant_id="tenant-1",
                    user_id="u-1",
                    channel_id="channel-1",
                    command_text="!run GP-114",
                    application_id="app-1",
                    interaction_token="token-1",
                    command_params=None,
                )
            )

        interaction_followup_mock.assert_called_once_with(
            application_id="app-1",
            interaction_token="token-1",
            content="Posted response in a follow-up thread.",
            ephemeral=False,
            components=None,
            reply_to_message_id=None,
            channel_id="channel-1",
        )
        upsert_context_mock.assert_called_once()
        self.assertEqual(upsert_context_mock.call_args.kwargs["context_type"], "decision_gate")
        self.assertEqual(upsert_context_mock.call_args.kwargs["thread_channel_id"], "thread-gp114")
        self.assertEqual(upsert_context_mock.call_args.kwargs["issue_key"], "GP-114")

        gateway_settings = SimpleNamespace(secrets_encryption_key="enc")
        with patch("orchestrator.core.discord.gateway_listener.create_session_factory", return_value=lambda: nullcontext(session)):
            listener = DiscordGatewayListener(settings=gateway_settings)
        listener._find_tenant_for_channel = MagicMock(return_value=tenant)

        gateway_response = SimpleNamespace(command="reply", message="ok", data={"recheck_required": False})
        with (
            patch(
                "orchestrator.core.discord.gateway_listener.resolve_followup_context",
                return_value=SimpleNamespace(context_type="decision_gate", issue_key="GP-114"),
            ),
            patch("orchestrator.core.discord.gateway_listener.execute_tenant_discord_command", return_value=gateway_response) as execute_mock,
            patch("orchestrator.core.discord.gateway_listener.resolve_tenant_jira_browse_base_url", return_value="https://jira.example.com"),
            patch("orchestrator.core.discord.gateway_listener.build_command_followup_message", return_value="ok"),
            patch("orchestrator.core.discord.gateway_listener.DiscordApiClient") as gateway_client_cls,
        ):
            listener._handle_message_create(
                {
                    "author": {"id": "u-1"},
                    "channel_id": "thread-gp114",
                    "content": "Dependencies: AVFoundation permissions and Vision model loading risk",
                    "attachments": [],
                },
                bot_token="token",
            )

        payload = execute_mock.call_args.kwargs["payload"]
        self.assertEqual(payload.command, "!reply")
        self.assertEqual(payload.command_params["issue_key"], "GP-114")
        self.assertEqual(
            payload.command_params["reply_text"],
            "Dependencies: AVFoundation permissions and Vision model loading risk",
        )
        gateway_client_cls.return_value.post_message.assert_called_once()


if __name__ == "__main__":
    unittest.main()
