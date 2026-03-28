import unittest
import json
from collections import Counter
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import MagicMock, patch

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect
from sqlalchemy import text

from orchestrator.storage.migrations import run_migrations


class MigrationTests(unittest.TestCase):
    def _alembic_upgrade(self, database_url: str, revision: str) -> None:
        root = Path(__file__).resolve().parents[1]
        config = Config(str(root / "alembic.ini"))
        config.attributes["configure_logger"] = False
        config.set_main_option(
            "script_location",
            str(root / "orchestrator" / "storage" / "migrations"),
        )
        config.set_main_option("sqlalchemy.url", database_url)
        command.upgrade(config, revision)

    def test_revision_graph_has_single_unique_head(self) -> None:
        root = Path(__file__).resolve().parents[1]
        config = Config(str(root / "alembic.ini"))
        config.attributes["configure_logger"] = False
        config.set_main_option(
            "script_location",
            str(root / "orchestrator" / "storage" / "migrations"),
        )
        script = ScriptDirectory.from_config(config)

        revision_ids: list[str] = []
        for path in sorted((root / "orchestrator" / "storage" / "migrations" / "versions").glob("*.py")):
            contents = path.read_text(encoding="utf-8")
            for line in contents.splitlines():
                if line.startswith("revision = "):
                    revision_ids.append(line.split("=", maxsplit=1)[1].strip().strip('"'))
                    break

        duplicates = {revision_id: count for revision_id, count in Counter(revision_ids).items() if count > 1}
        self.assertEqual(duplicates, {})
        self.assertEqual(script.get_heads(), ["20260328_0045"])

    def test_run_migrations_repairs_legacy_stream_only_0040_head(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            database_url = f"sqlite:///{tmp_dir}/test.db"
            self._alembic_upgrade(database_url, "20260323_0038")

            engine = create_engine(database_url)
            with engine.begin() as connection:
                connection.execute(text("DELETE FROM alembic_version"))
                connection.execute(text("INSERT INTO alembic_version(version_num) VALUES ('20260327_0040')"))
                connection.execute(
                    text(
                        """
                        CREATE TABLE run_stream_events (
                            stream_offset INTEGER PRIMARY KEY,
                            event_kind VARCHAR(32) NOT NULL,
                            tenant_id VARCHAR(128) NOT NULL,
                            project_id VARCHAR(128),
                            run_id VARCHAR(64),
                            issue_key VARCHAR(64),
                            agent_id VARCHAR(128),
                            event_type VARCHAR(64),
                            invocation_id VARCHAR(64),
                            channel VARCHAR(64),
                            command VARCHAR(128),
                            working_dir VARCHAR(1024),
                            stage VARCHAR(64),
                            attempt INTEGER,
                            stream VARCHAR(16),
                            message TEXT,
                            recorded_at DATETIME NOT NULL
                        )
                        """
                    )
                )
                for ddl in (
                    "CREATE INDEX ix_run_stream_events_event_kind ON run_stream_events (event_kind)",
                    "CREATE INDEX ix_run_stream_events_tenant_id ON run_stream_events (tenant_id)",
                    "CREATE INDEX ix_run_stream_events_project_id ON run_stream_events (project_id)",
                    "CREATE INDEX ix_run_stream_events_run_id ON run_stream_events (run_id)",
                    "CREATE INDEX ix_run_stream_events_agent_id ON run_stream_events (agent_id)",
                    "CREATE INDEX ix_run_stream_events_event_type ON run_stream_events (event_type)",
                    "CREATE INDEX ix_run_stream_events_invocation_id ON run_stream_events (invocation_id)",
                    "CREATE INDEX ix_run_stream_events_channel ON run_stream_events (channel)",
                    "CREATE INDEX ix_run_stream_events_command ON run_stream_events (command)",
                    "CREATE INDEX ix_run_stream_events_stage ON run_stream_events (stage)",
                    "CREATE INDEX ix_run_stream_events_recorded_at ON run_stream_events (recorded_at)",
                    "CREATE INDEX ix_run_stream_events_run_id_stream_offset ON run_stream_events (run_id, stream_offset)",
                    "CREATE INDEX ix_run_stream_events_tenant_id_stream_offset ON run_stream_events (tenant_id, stream_offset)",
                ):
                    connection.execute(text(ddl))

            run_migrations(database_url=database_url)

            inspector = inspect(engine)
            self.assertIn("tenant_users", inspector.get_table_names())
            self.assertIn("tenant_user_discord_identities", inspector.get_table_names())
            with engine.begin() as connection:
                versions = connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
            self.assertEqual(versions, ["20260328_0045"])

    def test_run_migrations_repairs_legacy_stream_only_0039_head(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            database_url = f"sqlite:///{tmp_dir}/test.db"
            self._alembic_upgrade(database_url, "20260323_0038")

            engine = create_engine(database_url)
            with engine.begin() as connection:
                connection.execute(text("DELETE FROM alembic_version"))
                connection.execute(text("INSERT INTO alembic_version(version_num) VALUES ('20260327_0039')"))
                connection.execute(
                    text(
                        """
                        CREATE TABLE run_stream_events (
                            stream_offset INTEGER PRIMARY KEY,
                            event_kind VARCHAR(32) NOT NULL,
                            tenant_id VARCHAR(128) NOT NULL,
                            project_id VARCHAR(128),
                            run_id VARCHAR(64),
                            issue_key VARCHAR(64),
                            agent_id VARCHAR(128),
                            event_type VARCHAR(64),
                            invocation_id VARCHAR(64),
                            channel VARCHAR(64),
                            command VARCHAR(128),
                            working_dir VARCHAR(1024),
                            stage VARCHAR(64),
                            attempt INTEGER,
                            stream VARCHAR(16),
                            message TEXT,
                            recorded_at DATETIME NOT NULL
                        )
                        """
                    )
                )

            run_migrations(database_url=database_url)

            inspector = inspect(engine)
            self.assertIn("tenant_users", inspector.get_table_names())
            self.assertIn("tenant_user_discord_identities", inspector.get_table_names())
            with engine.begin() as connection:
                versions = connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
            self.assertEqual(versions, ["20260328_0045"])

    def test_run_migrations_disables_alembic_logger_reconfiguration(self) -> None:
        fake_config = MagicMock()
        fake_config.attributes = {}
        with (
            patch("orchestrator.storage.migrations._normalize_repaired_top_revisions") as normalize_mock,
            patch("orchestrator.storage.migrations.Config", return_value=fake_config),
            patch("orchestrator.storage.migrations.command.upgrade") as upgrade_mock,
        ):
            run_migrations(database_url="sqlite:///tmp/test.db")

        normalize_mock.assert_called_once_with("sqlite:///tmp/test.db")
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
            self.assertIn("worker_runtime_states", inspector.get_table_names())
            knowledge_fact_columns = {column["name"]: column for column in inspector.get_columns("knowledge_facts")}
            self.assertEqual(getattr(knowledge_fact_columns["slot_name"]["type"], "length", None), 128)

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

    def test_tenant_identity_migration_uses_boolean_defaults_for_postgres(self) -> None:
        migration_file = (
            Path(__file__).resolve().parents[1]
            / "orchestrator"
            / "storage"
            / "migrations"
            / "versions"
            / "20260327_0039_tenant_identity_and_invites.py"
        )
        contents = migration_file.read_text(encoding="utf-8")

        self.assertIn(
            'sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true())',
            contents,
        )
        self.assertIn(
            'sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default=sa.false())',
            contents,
        )
        self.assertNotIn('server_default=sa.text("1")', contents)
        self.assertNotIn('server_default=sa.text("0")', contents)

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

    def test_run_migrations_accepts_database_stamped_with_merged_20260327_0043(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            database_url = f"sqlite:///{tmp_dir}/test.db"
            run_migrations(database_url=database_url)

            engine = create_engine(database_url)
            with engine.begin() as connection:
                connection.execute(text("UPDATE alembic_version SET version_num = '20260327_0043'"))

            run_migrations(database_url=database_url)

            with engine.begin() as connection:
                current_revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()

            self.assertEqual(current_revision, "20260327_0043")
