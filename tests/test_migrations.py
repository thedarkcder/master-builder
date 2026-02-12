import unittest
from tempfile import TemporaryDirectory
from pathlib import Path

from sqlalchemy import create_engine, inspect

from orchestrator.storage.migrations import run_migrations


class MigrationTests(unittest.TestCase):
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
