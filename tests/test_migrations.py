import unittest
import json
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine, inspect
from sqlalchemy import text

from orchestrator.storage.migrations import run_migrations


class MigrationTests(unittest.TestCase):
    def test_run_migrations_disables_alembic_logger_reconfiguration(self) -> None:
        fake_config = MagicMock()
        fake_config.attributes = {}
        with (
            patch("orchestrator.storage.migrations.Config", return_value=fake_config),
            patch("orchestrator.storage.migrations.command.upgrade") as upgrade_mock,
        ):
            run_migrations(database_url="sqlite:///tmp/test.db")

        self.assertEqual(fake_config.attributes.get("configure_logger"), False)
        upgrade_mock.assert_called_once_with(fake_config, "head")

    def test_migrations_create_expected_tables(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            database_url = f"sqlite:///{tmp_dir}/test.db"

            run_migrations(database_url=database_url)

            engine = create_engine(database_url)
            inspector = inspect(engine)

            self.assertIn("tenants", inspector.get_table_names())
            self.assertIn("jira_oauth_connections", inspector.get_table_names())
            self.assertIn("runs", inspector.get_table_names())
            self.assertIn("run_locks", inspector.get_table_names())
            self.assertIn("webhook_deliveries", inspector.get_table_names())
            self.assertIn("repo_bootstrap_states", inspector.get_table_names())
            self.assertIn("managed_secrets", inspector.get_table_names())
            self.assertIn("agent_lifecycle_events", inspector.get_table_names())
            self.assertIn("knowledge_jira_sync_runtime_states", inspector.get_table_names())
            self.assertIn("knowledge_jira_sync_project_states", inspector.get_table_names())
            self.assertIn("discord_command_sync_runtime_states", inspector.get_table_names())
            self.assertIn("project_automations", inspector.get_table_names())
            self.assertIn("project_automation_executions", inspector.get_table_names())

            automation_indexes = {index["name"] for index in inspector.get_indexes("project_automations")}
            execution_indexes = {index["name"] for index in inspector.get_indexes("project_automation_executions")}

            self.assertIn("ix_project_automations_due_scan", automation_indexes)
            self.assertIn("ix_project_automation_executions_due_scan", execution_indexes)
            self.assertIn("ix_project_automation_executions_automation_history", execution_indexes)

    def test_initial_migration_uses_boolean_default_for_tenants_enabled(self) -> None:
        migration_file = (
            Path(__file__).resolve().parents[1]
            / "orchestrator"
            / "storage"
            / "migrations"
            / "versions"
            / "20260206_0001_initial.py"
        )
        contents = migration_file.read_text(encoding="utf-8")

        self.assertIn(
            'sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.true())',
            contents,
        )
        self.assertNotIn('server_default=sa.text("1")', contents)

    def test_followup_context_identity_migration_adds_explicit_routing_columns(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            database_url = f"sqlite:///{tmp_dir}/test.db"
            run_migrations(database_url=database_url)

            engine = create_engine(database_url)
            inspector = inspect(engine)

            followup_columns = {column["name"] for column in inspector.get_columns("followup_contexts")}
            followup_indexes = {index["name"] for index in inspector.get_indexes("followup_contexts")}

            self.assertIn("owner_user_id", followup_columns)
            self.assertIn("origin_command", followup_columns)
            self.assertIn("ix_followup_contexts_tenant_status_owner", followup_indexes)

    def test_decision_state_migration_is_idempotent_when_tables_already_exist(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            database_url = f"sqlite:///{tmp_dir}/test.db"
            run_migrations(database_url=database_url)

            engine = create_engine(database_url)
            with engine.begin() as connection:
                connection.execute(text("UPDATE alembic_version SET version_num = '20260309_0021'"))

            run_migrations(database_url=database_url)

            inspector = inspect(engine)
            self.assertIn("decision_cases", inspector.get_table_names())
            self.assertIn("decision_cycles", inspector.get_table_names())
            self.assertIn("decision_events", inspector.get_table_names())
            self.assertIn("decision_effects_outbox", inspector.get_table_names())

    def test_orchestrated_session_migration_is_idempotent_when_column_already_exists(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            database_url = f"sqlite:///{tmp_dir}/test.db"
            run_migrations(database_url=database_url)

            engine = create_engine(database_url)
            with engine.begin() as connection:
                connection.execute(text("UPDATE alembic_version SET version_num = '20260310_0023'"))

            run_migrations(database_url=database_url)

            inspector = inspect(engine)
            run_columns = {column["name"] for column in inspector.get_columns("runs")}
            run_indexes = {index["name"] for index in inspector.get_indexes("runs")}
            self.assertIn("orchestrated_session_id", run_columns)
            self.assertIn("ix_runs_orchestrated_session_id", run_indexes)

    def test_pgvector_migration_serializes_extension_creation(self) -> None:
        migration_file = (
            Path(__file__).resolve().parents[1]
            / "orchestrator"
            / "storage"
            / "migrations"
            / "versions"
            / "20260311_0025_knowledge_pgvector_and_search.py"
        )
        contents = migration_file.read_text(encoding="utf-8")

        self.assertIn("pg_advisory_xact_lock", contents)
        self.assertIn("CREATE EXTENSION IF NOT EXISTS vector", contents)

    def test_live_voice_room_links_migration_rewrites_legacy_discord_config(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            database_url = f"sqlite:///{tmp_dir}/test.db"
            run_migrations(database_url=database_url)

            engine = create_engine(database_url)
            now = datetime.now(timezone.utc).isoformat()
            with engine.begin() as connection:
                connection.execute(
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
                        "tenant_id": "tenant-a",
                        "name": "Tenant A",
                        "is_enabled": True,
                        "jira_config": json.dumps({}),
                        "github_config": json.dumps({}),
                        "repos_config": json.dumps({}),
                        "policy_config": json.dumps({}),
                        "discord_config": json.dumps(
                            {
                                "live_voice_channel_id": "voice-room-1",
                                "live_voice_linked_text_channel_id": "text-room-1",
                            }
                        ),
                        "created_at": now,
                        "updated_at": now,
                    },
                )
                connection.execute(
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
                        "project_id": "project-a",
                        "tenant_id": "tenant-a",
                        "name": "Project A",
                        "github_repository": "https://github.com/example/repo",
                        "jira_project_key": "MAB",
                        "policy_overrides": json.dumps({}),
                        "environment": json.dumps({}),
                        "secret_refs": json.dumps({}),
                        "discord_config": json.dumps(
                            {
                                "live_voice_channel_id": "voice-room-1",
                                "live_voice_linked_text_channel_id": "text-room-1",
                                "live_voice_room_links": {
                                    "voice-room-1": "text-room-1",
                                    "voice-room-2": "text-room-2",
                                },
                            }
                        ),
                        "is_archived": False,
                        "created_at": now,
                        "updated_at": now,
                    },
                )
                connection.execute(text("UPDATE alembic_version SET version_num = '20260322_0033'"))

            run_migrations(database_url=database_url)

            with engine.begin() as connection:
                tenant_config = json.loads(
                    connection.execute(
                        text("SELECT discord_config FROM tenants WHERE tenant_id = 'tenant-a'")
                    ).scalar_one()
                )
                project_config = json.loads(
                    connection.execute(
                        text("SELECT discord_config FROM projects WHERE project_id = 'project-a'")
                    ).scalar_one()
                )

            self.assertEqual(
                tenant_config,
                {
                    "live_voice_room_links": {
                        "voice-room-1": "text-room-1",
                    }
                },
            )
            self.assertEqual(
                project_config,
                {
                    "live_voice_room_links": {
                        "voice-room-1": "text-room-1",
                        "voice-room-2": "text-room-2",
                    }
                },
            )
