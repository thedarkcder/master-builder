from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import select

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

    def test_project_migration_backfills_default_project_from_existing_tenant(self) -> None:
        self._alembic_upgrade("20260207_0006")
        session_factory = create_session_factory(database_url=self.database_url)
        now = datetime.now(timezone.utc)
        with session_factory() as session:
            session.add(
                Tenant(
                    tenant_id="tenant-one",
                    name="Tenant One",
                    is_enabled=True,
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
            )
            session.commit()

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
