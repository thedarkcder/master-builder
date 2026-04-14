from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from orchestrator.api.main import create_app
from orchestrator.core.discord.command_sync_status import DiscordCommandSyncStatusSnapshot


class AdminDiscordCommandsRouteTests(unittest.TestCase):
    def test_status_endpoint_returns_current_sync_status(self) -> None:
        status = DiscordCommandSyncStatusSnapshot(
            synced=True,
            healthy=True,
            interaction_ingress_ready=True,
            bot_token_configured=True,
            guild_id_configured=True,
            guild_id="guild-1",
            application_id="app-1",
            command_count=14,
        )

        with (
            patch("orchestrator.api.main.run_migrations"),
            patch("orchestrator.api.main.register_discord_command_executor"),
            patch("orchestrator.api.main.sync_discord_guild_commands"),
            patch(
                "orchestrator.api.routes.admin_discord_commands.get_discord_command_sync_status",
                return_value=status,
            ),
        ):
            app = create_app()
            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get("/api/admin/discord/commands/status", auth=("admin", "change-me"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "synced": True,
                "healthy": True,
                "interaction_ingress_ready": True,
                "bot_token_configured": True,
                "guild_id_configured": True,
                "last_attempt_at": None,
                "last_success_at": None,
                "last_failure_reason": None,
                "last_error": None,
                "guild_id": "guild-1",
                "application_id": "app-1",
                "command_count": 14,
            },
        )

    def test_sync_endpoint_runs_sync_and_returns_updated_status(self) -> None:
        status = DiscordCommandSyncStatusSnapshot(
            synced=False,
            healthy=False,
            interaction_ingress_ready=True,
            bot_token_configured=False,
            guild_id_configured=False,
            last_failure_reason="missing_bot_token",
            command_count=0,
        )

        with (
            patch("orchestrator.api.main.run_migrations"),
            patch("orchestrator.api.main.register_discord_command_executor"),
            patch("orchestrator.api.main.sync_discord_guild_commands"),
            patch("orchestrator.api.routes.admin_discord_commands.sync_discord_guild_commands") as sync_mock,
            patch(
                "orchestrator.api.routes.admin_discord_commands.get_discord_command_sync_status",
                return_value=status,
            ),
        ):
            app = create_app()
            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post("/api/admin/discord/commands/sync", auth=("admin", "change-me"))

        self.assertEqual(response.status_code, 200)
        sync_mock.assert_called_once()
        self.assertEqual(response.json()["last_failure_reason"], "missing_bot_token")
