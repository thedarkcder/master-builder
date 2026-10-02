from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from orchestrator.api.discord.shared.channel_scope_repository import (
    SqlAlchemyDiscordChannelScopeRepository,
)


class DiscordAdapterContractTests(unittest.TestCase):
    def test_scope_repository_returns_none_when_channel_unmapped(self) -> None:
        repository = SqlAlchemyDiscordChannelScopeRepository()
        tenant = SimpleNamespace(tenant_id="tenant-1")

        with patch(
            "orchestrator.api.discord.shared.channel_scope_repository.resolve_project_for_discord_channel",
            return_value=None,
        ):
            scope = repository.resolve_project_scope(
                session=SimpleNamespace(),
                tenant=tenant,
                channel_id="discord-unmapped",
            )

        self.assertIsNone(scope)

    def test_scope_repository_returns_resolved_scope_for_mapped_channel(self) -> None:
        repository = SqlAlchemyDiscordChannelScopeRepository()
        tenant = SimpleNamespace(tenant_id="tenant-1")
        project = SimpleNamespace(project_id="project-1", jira_project_key="DEMO")

        with patch(
            "orchestrator.api.discord.shared.channel_scope_repository.resolve_project_for_discord_channel",
            return_value=project,
        ):
            scope = repository.resolve_project_scope(
                session=SimpleNamespace(),
                tenant=tenant,
                channel_id="discord-exampleapp",
            )

        self.assertIsNotNone(scope)
        assert scope is not None
        self.assertEqual(scope.tenant_id, "tenant-1")
        self.assertEqual(scope.project_id, "project-1")
        self.assertEqual(scope.jira_project_key, "DEMO")


if __name__ == "__main__":
    unittest.main()
