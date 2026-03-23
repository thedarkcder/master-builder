from __future__ import annotations

from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import pytest

from orchestrator.core.config import get_settings
from orchestrator.api.discord.ingress.executor import register_discord_command_executor
from orchestrator.core.discord.gateway_listener import DiscordGatewayListener
from tests.production_path_support import (
    DeferredTaskHarness,
    FakeDiscordApiClient,
    clear_runtime_environment,
    configure_runtime_environment,
    seed_core_runtime_state,
    session_factory_for,
    upsert_platform_secret,
)

pytestmark = pytest.mark.production_path


class GatewayListenerProductionPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.database_url, _ = configure_runtime_environment(
            temp_dir=self.temp_dir,
            database_name="gateway_listener_production.db",
        )
        self.session_factory = session_factory_for(self.database_url)
        seed_core_runtime_state(self.session_factory)
        upsert_platform_secret(
            session_factory=self.session_factory,
            secret_ref="DISCORD_BOT_TOKEN",
            plaintext_value="discord-token",
        )
        register_discord_command_executor()
        self.settings = get_settings()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        clear_runtime_environment()

    def _listener(self) -> DiscordGatewayListener:
        listener = DiscordGatewayListener(settings=self.settings)
        listener._session_factory = self.session_factory
        return listener

    def test_interaction_help_runs_real_deferred_followup(self) -> None:
        listener = self._listener()
        harness = DeferredTaskHarness()
        discord_client = FakeDiscordApiClient(bot_token="discord-token")
        payload = {
            "id": "interaction-1",
            "type": 2,
            "application_id": "app-1",
            "token": "token-1",
            "channel_id": "discord-channel-1",
            "member": {"user": {"id": "u-1"}},
            "data": {"name": "help", "options": []},
        }
        callback_payloads: list[dict[str, object]] = []

        def _callback(**kwargs):  # noqa: ANN003
            callback_payloads.append(dict(kwargs))

        with (
            patch("orchestrator.core.discord.gateway_listener.asyncio.create_task", new=harness.create_task),
            patch("orchestrator.core.discord.gateway_listener.send_discord_interaction_callback", new=_callback),
            patch(
                "orchestrator.api.discord.interactions.followup_transport.DiscordApiClient",
                return_value=discord_client,
            ),
        ):
            import asyncio

            asyncio.run(listener._handle_interaction_create(payload))
            harness.wait()

        self.assertEqual(len(callback_payloads), 1)
        self.assertEqual(callback_payloads[0]["interaction_id"], "interaction-1")
        self.assertEqual(len(discord_client.created_threads), 1)
        self.assertEqual(discord_client.created_threads[0]["channel_id"], "discord-channel-1")
        self.assertEqual(len(discord_client.posted_messages), 2)
        self.assertIn("!help", str(discord_client.posted_messages[0]["content"]))
        self.assertEqual(discord_client.posted_messages[1]["channel_id"], "thread-1001")

    def test_message_create_runs_real_command_pipeline(self) -> None:
        listener = self._listener()
        discord_client = FakeDiscordApiClient(bot_token="discord-token")
        payload = {
            "id": "message-1",
            "channel_id": "discord-channel-1",
            "content": "!help",
            "author": {"id": "u-1", "bot": False},
            "attachments": [],
        }

        with patch("orchestrator.core.discord.gateway_listener.DiscordApiClient", return_value=discord_client):
            listener._handle_message_create(payload, "discord-token")

        self.assertEqual(len(discord_client.posted_messages), 1)
        self.assertEqual(discord_client.posted_messages[0]["channel_id"], "discord-channel-1")
        self.assertIn("!run", str(discord_client.posted_messages[0]["content"]))
