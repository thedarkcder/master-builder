import unittest
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
