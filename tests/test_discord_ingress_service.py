from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from orchestrator.api.discord.ingress.service import (
    DiscordIngressDependencies,
    DiscordIngressHandlers,
    execute_tenant_command_ingress,
)
from orchestrator.api.schemas import DiscordCommandRequest, DiscordCommandResponse


def _deps() -> DiscordIngressDependencies:
    handlers = DiscordIngressHandlers(
        simple=MagicMock(),
        ask=MagicMock(),
        bug_gap=MagicMock(),
        issues=MagicMock(),
        run_control=MagicMock(),
    )
    return DiscordIngressDependencies(
        get_tenant=MagicMock(),
        resolve_discord_command=MagicMock(),
        assert_channel_scope=MagicMock(),
        assert_sensitive_command_permission=MagicMock(),
        resolve_scope=MagicMock(),
        handlers=handlers,
    )


class DiscordIngressServiceTests(unittest.TestCase):
    def test_execute_tenant_command_ingress_commits_after_success(self) -> None:
        session = MagicMock()
        response = DiscordCommandResponse(ok=True, command="status", message="ok", data={})

        with patch(
            "orchestrator.api.discord.ingress.service.execute_tenant_command",
            return_value=response,
        ) as execute_mock:
            result = execute_tenant_command_ingress(
                tenant_id="tenant-a",
                payload=DiscordCommandRequest(user_id="u1", channel_id="c1", command="!status"),
                session=session,
                deps=_deps(),
            )

        self.assertIs(result, response)
        execute_mock.assert_called_once()
        session.commit.assert_called_once_with()
        session.rollback.assert_not_called()

    def test_execute_tenant_command_ingress_rolls_back_on_failure(self) -> None:
        session = MagicMock()

        with patch(
            "orchestrator.api.discord.ingress.service.execute_tenant_command",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                execute_tenant_command_ingress(
                    tenant_id="tenant-a",
                    payload=DiscordCommandRequest(user_id="u1", channel_id="c1", command="!status"),
                    session=session,
                    deps=_deps(),
                )

        session.rollback.assert_called_once_with()
        session.commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
