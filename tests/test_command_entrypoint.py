from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from orchestrator.api.commands.entrypoint import execute_tenant_discord_command
from orchestrator.api.commands.entrypoint import (
    execute_tenant_discord_ingress_command,
    execute_tenant_jira_comment_command,
)
from orchestrator.api.commands.executor_registry import (
    clear_tenant_command_executor,
    register_tenant_command_executor,
)
from orchestrator.api.schemas import DiscordCommandRequest, DiscordCommandResponse


class CommandEntrypointTests(unittest.TestCase):
    def tearDown(self) -> None:
        clear_tenant_command_executor()

    def test_execute_tenant_discord_command_requires_registered_executor_when_bootstrap_fails(
        self,
    ) -> None:
        clear_tenant_command_executor()
        with patch(
            "orchestrator.api.discord.ingress.executor.register_discord_command_executor",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertRaises(HTTPException) as ctx:
                execute_tenant_discord_command(
                    tenant_id="tenant-a",
                    payload=DiscordCommandRequest(
                        user_id="u1", channel_id="c1", command="!status"
                    ),
                    session=MagicMock(),
                )
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(
            ctx.exception.detail, "Discord command executor is not registered"
        )

    def test_execute_tenant_discord_command_bootstraps_executor_when_missing(
        self,
    ) -> None:
        clear_tenant_command_executor()
        response = DiscordCommandResponse(
            ok=True, command="status", message="ok", data={}
        )
        executor = MagicMock(return_value=response)

        def _register() -> None:
            register_tenant_command_executor(executor)

        with patch(
            "orchestrator.api.discord.ingress.executor.register_discord_command_executor",
            side_effect=_register,
        ):
            result = execute_tenant_discord_command(
                tenant_id="tenant-a",
                payload=DiscordCommandRequest(
                    user_id="u1", channel_id="c1", command="!status"
                ),
                session=MagicMock(),
            )

        self.assertIs(result, response)
        executor.assert_called_once()

    def test_execute_tenant_discord_command_delegates_to_registered_executor(
        self,
    ) -> None:
        response = DiscordCommandResponse(
            ok=True, command="status", message="ok", data={}
        )
        executor = MagicMock(return_value=response)
        register_tenant_command_executor(executor)

        result = execute_tenant_discord_command(
            tenant_id="tenant-a",
            payload=DiscordCommandRequest(
                user_id="u1", channel_id="c1", command="!status"
            ),
            session=MagicMock(),
            defer_seed_issues=True,
            require_ask_confirmation=True,
            ingress_source="discord",
        )

        self.assertIs(result, response)
        executor.assert_called_once()
        kwargs = executor.call_args.kwargs
        self.assertEqual(kwargs["tenant_id"], "tenant-a")
        self.assertTrue(kwargs["defer_seed_issues"])
        self.assertTrue(kwargs["require_ask_confirmation"])
        self.assertEqual(kwargs["ingress_source"], "discord")

    def test_execute_tenant_discord_ingress_command_forces_discord_source(self) -> None:
        response = DiscordCommandResponse(
            ok=True, command="status", message="ok", data={}
        )
        executor = MagicMock(return_value=response)
        register_tenant_command_executor(executor)

        execute_tenant_discord_ingress_command(
            tenant_id="tenant-a",
            payload=DiscordCommandRequest(
                user_id="u1", channel_id="c1", command="!status"
            ),
            session=MagicMock(),
        )
        self.assertEqual(executor.call_args.kwargs["ingress_source"], "discord")

    def test_execute_tenant_discord_ingress_command_rejects_non_discord_source(
        self,
    ) -> None:
        response = DiscordCommandResponse(
            ok=True, command="status", message="ok", data={}
        )
        executor = MagicMock(return_value=response)
        register_tenant_command_executor(executor)

        with self.assertRaises(HTTPException) as ctx:
            execute_tenant_discord_ingress_command(
                tenant_id="tenant-a",
                payload=DiscordCommandRequest(
                    user_id="u1", channel_id="c1", command="!status"
                ),
                session=MagicMock(),
                ingress_source="jira_comment",
            )
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("ingress_source='discord'", str(ctx.exception.detail))

    def test_execute_tenant_jira_comment_command_forces_jira_comment_source(
        self,
    ) -> None:
        response = DiscordCommandResponse(ok=True, command="ask", message="ok", data={})
        executor = MagicMock(return_value=response)
        register_tenant_command_executor(executor)

        execute_tenant_jira_comment_command(
            tenant_id="tenant-a",
            payload=DiscordCommandRequest(
                user_id="u1", channel_id=None, command="!ask test"
            ),
            session=MagicMock(),
        )
        self.assertEqual(executor.call_args.kwargs["ingress_source"], "jira_comment")

    def test_execute_tenant_jira_comment_command_rejects_non_jira_source(self) -> None:
        response = DiscordCommandResponse(ok=True, command="ask", message="ok", data={})
        executor = MagicMock(return_value=response)
        register_tenant_command_executor(executor)

        with self.assertRaises(HTTPException) as ctx:
            execute_tenant_jira_comment_command(
                tenant_id="tenant-a",
                payload=DiscordCommandRequest(
                    user_id="u1", channel_id=None, command="!ask test"
                ),
                session=MagicMock(),
                ingress_source="discord",
            )
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("ingress_source='jira_comment'", str(ctx.exception.detail))


if __name__ == "__main__":
    unittest.main()
