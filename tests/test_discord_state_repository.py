from __future__ import annotations

import unittest
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock

from orchestrator.api.discord.shared.state_repository import (
    resolve_project_for_discord_channel,
)
from orchestrator.core.discord.channel_tenant_index import (
    invalidate_discord_channel_tenant_index,
    resolve_tenant_for_discord_channel,
)
from orchestrator.core.pm.followup_context_service import upsert_followup_context
from tests.production_path_support import (
    clear_runtime_environment,
    configure_runtime_environment,
    seed_core_runtime_state,
    session_factory_for,
)


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


class DiscordStateRepositoryIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.database_url, _ = configure_runtime_environment(
            temp_dir=self.temp_dir,
            database_name="discord_state_repository.db",
        )
        self.session_factory = session_factory_for(self.database_url)
        seed_core_runtime_state(
            self.session_factory,
            discord_channel_id="project-home-1",
            project_discord_config={"channel_id": "project-home-1"},
        )
        invalidate_discord_channel_tenant_index()

    def tearDown(self) -> None:
        invalidate_discord_channel_tenant_index()
        self.temp_dir.cleanup()
        clear_runtime_environment()

    def test_active_followup_thread_resolves_tenant_and_project(self) -> None:
        with self.session_factory() as session:
            upsert_followup_context(
                session=session,
                tenant_id="example-workspace",
                project_id="example-workspace-default",
                context_type="ask_thread",
                channel_id="project-home-1",
                thread_channel_id="ask-thread-1",
                root_message_id="message-1",
                request_id="ask-1",
            )
            session.commit()

        with self.session_factory() as session:
            tenant = resolve_tenant_for_discord_channel(
                session=session, channel_id="ask-thread-1"
            )
            project = resolve_project_for_discord_channel(
                session=session,
                tenant_id="example-workspace",
                channel_id="ask-thread-1",
            )

        self.assertIsNotNone(tenant)
        assert tenant is not None
        self.assertEqual(tenant.tenant_id, "example-workspace")
        self.assertIsNotNone(project)
        assert project is not None
        self.assertEqual(project.project_id, "example-workspace-default")


if __name__ == "__main__":
    unittest.main()
