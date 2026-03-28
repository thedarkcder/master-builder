from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import select, text

from orchestrator.core.config import get_settings
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.models import Project, Tenant


class ProjectMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.database_url = f"sqlite:///{self.temp_dir.name}/migration_test.db"
        os.environ["ORCHESTRATOR_DATABASE_URL"] = self.database_url
        get_settings.cache_clear()
        reset_db_engine_cache()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        get_settings.cache_clear()
        reset_db_engine_cache()

    def _alembic_upgrade(self, revision: str) -> None:
        root = Path(__file__).resolve().parents[1]
        config = Config(str(root / "alembic.ini"))
        config.set_main_option(
            "script_location",
            str(root / "orchestrator" / "storage" / "migrations"),
        )
        config.set_main_option("sqlalchemy.url", self.database_url)
        command.upgrade(config, revision)

    def _insert_legacy_tenant(
        self,
        *,
        tenant_id: str,
        name: str,
        jira_config: dict[str, object],
        github_config: dict[str, object],
        repos_config: dict[str, object],
        policy_config: dict[str, object],
        discord_config: dict[str, object] | None,
        created_at: datetime,
        updated_at: datetime,
    ) -> None:
        session_factory = create_session_factory(database_url=self.database_url)
        with session_factory() as session:
            session.execute(
                text(
                    """
                    INSERT INTO tenants (
                        tenant_id, name, is_enabled, jira_config, github_config, repos_config,
                        policy_config, discord_config, created_at, updated_at
                    ) VALUES (
                        :tenant_id, :name, :is_enabled, :jira_config, :github_config, :repos_config,
                        :policy_config, :discord_config, :created_at, :updated_at
                    )
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "name": name,
                    "is_enabled": True,
                    "jira_config": json.dumps(jira_config),
                    "github_config": json.dumps(github_config),
                    "repos_config": json.dumps(repos_config),
                    "policy_config": json.dumps(policy_config),
                    "discord_config": json.dumps(discord_config) if discord_config is not None else None,
                    "created_at": created_at.isoformat(),
                    "updated_at": updated_at.isoformat(),
                },
            )
            session.commit()

    def _insert_legacy_project(
        self,
        *,
        project_id: str,
        tenant_id: str,
        name: str,
        github_repository: str,
        jira_project_key: str,
        discord_config: dict[str, object] | None,
        created_at: datetime,
        updated_at: datetime,
    ) -> None:
        session_factory = create_session_factory(database_url=self.database_url)
        with session_factory() as session:
            session.execute(
                text(
                    """
                    INSERT INTO projects (
                        project_id, tenant_id, name, github_repository, jira_project_key,
                        policy_overrides, environment, secret_refs, discord_config, is_archived,
                        created_at, updated_at
                    ) VALUES (
                        :project_id, :tenant_id, :name, :github_repository, :jira_project_key,
                        :policy_overrides, :environment, :secret_refs, :discord_config, :is_archived,
                        :created_at, :updated_at
                    )
                    """
                ),
                {
                    "project_id": project_id,
                    "tenant_id": tenant_id,
                    "name": name,
                    "github_repository": github_repository,
                    "jira_project_key": jira_project_key,
                    "policy_overrides": json.dumps({}),
                    "environment": json.dumps({}),
                    "secret_refs": json.dumps({}),
                    "discord_config": json.dumps(discord_config) if discord_config is not None else None,
                    "is_archived": False,
                    "created_at": created_at.isoformat(),
                    "updated_at": updated_at.isoformat(),
                },
            )
            session.commit()

    def test_project_migration_backfills_default_project_from_existing_tenant(self) -> None:
        self._alembic_upgrade("20260207_0006")
        session_factory = create_session_factory(database_url=self.database_url)
        now = datetime.now(timezone.utc)
        self._insert_legacy_tenant(
            tenant_id="tenant-one",
            name="Tenant One",
            jira_config={
                "project_keys": ["ABC"],
                "ready_statuses": ["Ready for Agent"],
            },
            github_config={
                "mode": "github_app",
                "installation_id": "123",
            },
            repos_config={
                "github_repository": "https://github.com/example/project-one",
            },
            policy_config={
                "allow_jira_transitions": False,
                "allow_pr_creation": True,
                "allow_label_mutations": True,
                "max_runtime_minutes": 30,
                "max_dev_test_review_loops": 2,
                "max_concurrent_runs": 2,
                "allowed_commands": [],
                "require_agents_md": False,
            },
            discord_config=None,
            created_at=now,
            updated_at=now,
        )

        self._alembic_upgrade("head")

        with session_factory() as session:
            projects = session.execute(select(Project).where(Project.tenant_id == "tenant-one")).scalars().all()
            self.assertEqual(len(projects), 1)
            project = projects[0]
            self.assertEqual(project.project_id, "tenant-one-default")
            self.assertEqual(project.name, "project-one")
            self.assertEqual(project.github_repository, "https://github.com/example/project-one")
            self.assertEqual(project.jira_project_key, "ABC")
            self.assertFalse(project.is_archived)

        # Upgrade to head again should not create duplicates.
        self._alembic_upgrade("head")
        with session_factory() as session:
            project_count = session.execute(
                select(Project).where(Project.tenant_id == "tenant-one")
            ).scalars().all()
            self.assertEqual(len(project_count), 1)

    def test_thread_channels_migrate_from_tenant_to_project_scope(self) -> None:
        self._alembic_upgrade("20260209_0011")
        session_factory = create_session_factory(database_url=self.database_url)
        now = datetime.now(timezone.utc)
        self._insert_legacy_tenant(
            tenant_id="tenant-threads",
            name="Tenant Threads",
            jira_config={"project_keys": ["THR"], "ready_statuses": ["To Do"]},
            github_config={"mode": "github_app", "installation_id": "123"},
            repos_config={"github_repository": "https://github.com/example/threads"},
            policy_config={
                "allow_jira_transitions": False,
                "allow_pr_creation": True,
                "allow_label_mutations": True,
                "max_runtime_minutes": 30,
                "max_dev_test_review_loops": 2,
                "max_concurrent_runs": 2,
                "allowed_commands": [],
                "require_agents_md": False,
            },
            discord_config={
                "channel_id": "discord-main",
                "ask_thread_channel_ids": ["discord-ask-thread-1"],
                "seed_followup_thread_channel_ids": ["discord-seed-thread-1"],
            },
            created_at=now,
            updated_at=now,
        )
        self._insert_legacy_project(
            project_id="tenant-threads-default",
            tenant_id="tenant-threads",
            name="threads",
            github_repository="https://github.com/example/threads",
            jira_project_key="THR",
            discord_config={"channel_id": "discord-main"},
            created_at=now,
            updated_at=now,
        )

        self._alembic_upgrade("head")

        with session_factory() as session:
            tenant = session.get(Tenant, "tenant-threads")
            self.assertIsNotNone(tenant)
            project = session.get(Project, "tenant-threads-default")
            self.assertIsNotNone(project)
            tenant_discord = dict(tenant.discord_config or {})
            project_discord = dict(project.discord_config or {})
            self.assertNotIn("ask_thread_channel_ids", tenant_discord)
            self.assertNotIn("seed_followup_thread_channel_ids", tenant_discord)
            self.assertIn("discord-ask-thread-1", project_discord.get("ask_thread_channel_ids", []))
            self.assertIn("discord-seed-thread-1", project_discord.get("seed_followup_thread_channel_ids", []))

    def test_thread_channels_keep_unresolved_tenant_entries_for_multi_project_tenants(self) -> None:
        self._alembic_upgrade("20260209_0011")
        session_factory = create_session_factory(database_url=self.database_url)
        now = datetime.now(timezone.utc)
        self._insert_legacy_tenant(
            tenant_id="tenant-multi-threads",
            name="Tenant Multi Threads",
            jira_config={"project_keys": ["APP", "API"], "ready_statuses": ["To Do"]},
            github_config={"mode": "github_app", "installation_id": "123"},
            repos_config={"github_repository": "https://github.com/example/multi"},
            policy_config={
                "allow_jira_transitions": False,
                "allow_pr_creation": True,
                "allow_label_mutations": True,
                "max_runtime_minutes": 30,
                "max_dev_test_review_loops": 2,
                "max_concurrent_runs": 2,
                "allowed_commands": [],
                "require_agents_md": False,
            },
            discord_config={
                "channel_id": "discord-main",
                "ask_thread_channel_ids": ["discord-app-thread", "discord-unmapped-thread"],
                "seed_followup_thread_channel_ids": ["discord-seed-thread", "discord-unmapped-seed-thread"],
                "seed_followups": [
                    {
                        "channel_ids": ["discord-app-thread", "discord-seed-thread"],
                        "issue_keys": ["APP-12"],
                    }
                ],
            },
            created_at=now,
            updated_at=now,
        )
        self._insert_legacy_project(
            project_id="tenant-multi-threads-app",
            tenant_id="tenant-multi-threads",
            name="app",
            github_repository="https://github.com/example/multi-app",
            jira_project_key="APP",
            discord_config={"channel_id": "discord-app"},
            created_at=now,
            updated_at=now,
        )
        self._insert_legacy_project(
            project_id="tenant-multi-threads-api",
            tenant_id="tenant-multi-threads",
            name="api",
            github_repository="https://github.com/example/multi-api",
            jira_project_key="API",
            discord_config={"channel_id": "discord-api"},
            created_at=now,
            updated_at=now,
        )

        self._alembic_upgrade("head")

        with session_factory() as session:
            tenant = session.get(Tenant, "tenant-multi-threads")
            self.assertIsNotNone(tenant)
            app_project = session.get(Project, "tenant-multi-threads-app")
            self.assertIsNotNone(app_project)
            tenant_discord = dict(tenant.discord_config or {})
            app_project_discord = dict(app_project.discord_config or {})
            self.assertIn("discord-app-thread", app_project_discord.get("ask_thread_channel_ids", []))
            self.assertIn("discord-seed-thread", app_project_discord.get("seed_followup_thread_channel_ids", []))
            self.assertEqual(
                tenant_discord.get("ask_thread_channel_ids"),
                ["discord-unmapped-thread"],
            )
            self.assertEqual(
                tenant_discord.get("seed_followup_thread_channel_ids"),
                ["discord-unmapped-seed-thread"],
            )
