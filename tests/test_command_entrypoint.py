from __future__ import annotations

import unittest
from unittest.mock import MagicMock
from unittest.mock import patch

from fastapi import HTTPException

from orchestrator.api.command_entrypoint import execute_tenant_discord_command
from orchestrator.api.command_executor_registry import clear_tenant_command_executor, register_tenant_command_executor
from orchestrator.api.schemas import DiscordCommandRequest, DiscordCommandResponse


class CommandEntrypointTests(unittest.TestCase):
    def tearDown(self) -> None:
        clear_tenant_command_executor()

    def test_execute_tenant_discord_command_requires_registered_executor(self) -> None:
        clear_tenant_command_executor()
        with patch("orchestrator.api.command_entrypoint.importlib.import_module", return_value=object()):
            with self.assertRaises(HTTPException) as ctx:
                execute_tenant_discord_command(
                    tenant_id="tenant-a",
                    payload=DiscordCommandRequest(user_id="u1", channel_id="c1", command="!status"),
                    session=MagicMock(),
                )
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.detail, "Discord command executor is not registered")

    def test_execute_tenant_discord_command_delegates_to_registered_executor(self) -> None:
        response = DiscordCommandResponse(ok=True, command="status", message="ok", data={})
        executor = MagicMock(return_value=response)
        register_tenant_command_executor(executor)

        result = execute_tenant_discord_command(
            tenant_id="tenant-a",
            payload=DiscordCommandRequest(user_id="u1", channel_id="c1", command="!status"),
            session=MagicMock(),
            defer_seed_issues=True,
            require_ask_confirmation=True,
            allow_plain_ask=True,
            ingress_source="discord",
        )

        self.assertIs(result, response)
        executor.assert_called_once()
        kwargs = executor.call_args.kwargs
        self.assertEqual(kwargs["tenant_id"], "tenant-a")
        self.assertTrue(kwargs["defer_seed_issues"])
        self.assertTrue(kwargs["require_ask_confirmation"])
        self.assertTrue(kwargs["allow_plain_ask"])
        self.assertEqual(kwargs["ingress_source"], "discord")


if __name__ == "__main__":
    unittest.main()
