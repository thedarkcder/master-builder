from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi import HTTPException

from orchestrator.api.commands.execution_service import CommandExecutionDependencies, execute_tenant_command
from orchestrator.api.schemas import DiscordCommandRequest, DiscordCommandResponse
from orchestrator.core.communications.command_pipeline import CommandScope


class CommandExecutionServiceTests(unittest.TestCase):
    def _deps(self) -> CommandExecutionDependencies:
        tenant = SimpleNamespace(tenant_id="tenant-a", is_enabled=True)
        return CommandExecutionDependencies(
            get_tenant=lambda _session, _tenant_id: tenant,
            parse_ingress_source=lambda raw: SimpleNamespace(value=raw),
            resolve_discord_command=lambda _tenant, _raw, _channel, _allow: ("!status", "status", []),
            assert_channel_scope=lambda _session, _tenant, _channel_id: None,
            assert_sensitive_command_permission=lambda _session, _tenant, _command, _user, _channel_id: None,
            resolve_scope=lambda _session, _tenant, _channel_id: CommandScope(project_keys=("PRJ",)),
            enrich_scope=lambda _session, _tenant, _command_name, _arguments, _payload, scope: scope,
            rewrite_raw_command=lambda _session, _tenant, payload, _policy: payload.command,
            allow_sensitive_command_bypass=lambda _session, _tenant, _command_name, _arguments, _payload: False,
            build_handler_registry=lambda _context: {"status": (lambda _ctx: DiscordCommandResponse(ok=True, command="status", message="ok"),)},
        )

    def test_rejects_unknown_tenant(self) -> None:
        deps = self._deps()
        deps = CommandExecutionDependencies(
            **{**deps.__dict__, "get_tenant": lambda _session, _tenant_id: None}
        )
        with self.assertRaises(HTTPException) as ctx:
            execute_tenant_command(
                tenant_id="tenant-a",
                payload=DiscordCommandRequest(user_id="u1", channel_id="c1", command="!status"),
                session=MagicMock(),
                defer_seed_issues=False,
                require_ask_confirmation=False,
                allow_plain_ask=False,
                ingress_source="discord",
                deps=deps,
            )
        self.assertEqual(ctx.exception.status_code, 404)

    def test_enforces_discord_channel_scope_for_discord_ingress(self) -> None:
        scope_assert = MagicMock()
        deps = self._deps()
        deps = CommandExecutionDependencies(
            **{**deps.__dict__, "assert_channel_scope": scope_assert}
        )
        execute_tenant_command(
            tenant_id="tenant-a",
            payload=DiscordCommandRequest(user_id="u1", channel_id="c1", command="!status"),
            session=MagicMock(),
            defer_seed_issues=False,
            require_ask_confirmation=False,
            allow_plain_ask=False,
            ingress_source="discord",
            deps=deps,
        )
        scope_assert.assert_called_once()

    def test_skips_channel_scope_for_jira_comment_ingress(self) -> None:
        scope_assert = MagicMock()
        deps = self._deps()
        deps = CommandExecutionDependencies(
            **{**deps.__dict__, "assert_channel_scope": scope_assert}
        )
        execute_tenant_command(
            tenant_id="tenant-a",
            payload=DiscordCommandRequest(user_id="u1", channel_id=None, command="!status"),
            session=MagicMock(),
            defer_seed_issues=False,
            require_ask_confirmation=False,
            allow_plain_ask=False,
            ingress_source="jira_comment",
            deps=deps,
        )
        scope_assert.assert_not_called()

    def test_enriches_scope_before_handler_execution(self) -> None:
        captured_scope: list[CommandScope] = []
        deps = self._deps()
        deps = CommandExecutionDependencies(
            **{
                **deps.__dict__,
                "enrich_scope": lambda _session, _tenant, _command_name, _arguments, _payload, _scope: CommandScope(
                    project_id="project-a",
                    project_keys=("PRJ",),
                    channel_id="c1",
                ),
                "build_handler_registry": lambda _context: {
                    "status": (
                        lambda ctx: captured_scope.append(ctx.scope)
                        or DiscordCommandResponse(ok=True, command="status", message="ok"),
                    )
                },
            }
        )

        execute_tenant_command(
            tenant_id="tenant-a",
            payload=DiscordCommandRequest(user_id="u1", channel_id="c1", command="!status"),
            session=MagicMock(),
            defer_seed_issues=False,
            require_ask_confirmation=False,
            allow_plain_ask=False,
            ingress_source="discord",
            deps=deps,
        )

        self.assertEqual(captured_scope[0].project_id, "project-a")

    def test_discord_policy_overrides_route_flags(self) -> None:
        captured: dict[str, object] = {}

        def _resolve_discord_command(_tenant, _raw, _channel, allow_plain):  # noqa: ANN001
            captured["allow_plain_ask"] = allow_plain
            return "!ask test", "ask", ["test"]

        def _ask_handler(ctx):  # noqa: ANN001
            captured["require_ask_confirmation"] = bool(ctx.flags.get("require_ask_confirmation"))
            return DiscordCommandResponse(ok=True, command="ask", message="ok")

        deps = self._deps()
        deps = CommandExecutionDependencies(
            **{
                **deps.__dict__,
                "resolve_discord_command": _resolve_discord_command,
                "build_handler_registry": lambda _context: {"ask": (_ask_handler,)},
            }
        )

        execute_tenant_command(
            tenant_id="tenant-a",
            payload=DiscordCommandRequest(user_id="u1", channel_id="c1", command="plain text"),
            session=MagicMock(),
            defer_seed_issues=False,
            require_ask_confirmation=False,
            allow_plain_ask=False,
            ingress_source="discord",
            deps=deps,
        )

        self.assertTrue(captured["allow_plain_ask"])
        self.assertTrue(captured["require_ask_confirmation"])


if __name__ == "__main__":
    unittest.main()
