from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from orchestrator.api.discord.shared.state_repository import resolve_project_for_discord_channel


class DiscordStateRepositoryTests(unittest.TestCase):
    def test_resolve_project_for_discord_channel_matches_pm_room_channel(self) -> None:
        session = MagicMock()
        project = SimpleNamespace(
            tenant_id="tenant-1",
            is_archived=False,
            discord_config={"pm_room_channel_ids": ["pm-room-1"]},
        )
        tenant = SimpleNamespace(discord_config={})
        session.execute.return_value.scalars.return_value.all.return_value = [project]
        session.get.return_value = tenant

        resolved = resolve_project_for_discord_channel(
            session=session,
            tenant_id="tenant-1",
            channel_id="pm-room-1",
        )

        self.assertIs(resolved, project)


if __name__ == "__main__":
    unittest.main()
