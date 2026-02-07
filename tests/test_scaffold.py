import unittest
from tempfile import TemporaryDirectory

from sqlalchemy import text

from orchestrator.api.main import create_app
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations


class ScaffoldFoundationTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_db_engine_cache()

    def test_create_app_registers_foundation_routes(self) -> None:
        app = create_app()
        paths = {route.path for route in app.routes}

        self.assertIn("/health", paths)
        self.assertIn("/api/admin/tenants", paths)
        self.assertIn("/jira/webhook/{tenant_id}", paths)
        self.assertIn("/runs/{run_id}", paths)

    def test_session_factory_works_after_initial_migration(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            database_url = f"sqlite:///{tmp_dir}/scaffold.db"
            run_migrations(database_url=database_url)
            session_factory = create_session_factory(database_url=database_url)

            with session_factory() as session:
                has_tenants_table = session.execute(
                    text(
                        "SELECT COUNT(*) FROM sqlite_master "
                        "WHERE type = 'table' AND name = 'tenants'"
                    )
                ).scalar_one()
                has_runs_table = session.execute(
                    text(
                        "SELECT COUNT(*) FROM sqlite_master "
                        "WHERE type = 'table' AND name = 'runs'"
                    )
                ).scalar_one()

            self.assertEqual(has_tenants_table, 1)
            self.assertEqual(has_runs_table, 1)
