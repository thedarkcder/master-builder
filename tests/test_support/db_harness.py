from __future__ import annotations

import os
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from fastapi.testclient import TestClient

from orchestrator.core.config import get_settings
from orchestrator.storage.db import reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations


class SqliteTemplateDbTestCase(unittest.TestCase):
    """Build one migrated SQLite template per class, then copy per test."""

    _db_workspace: TemporaryDirectory[str]
    _template_db_path: Path
    _template_database_url: str

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._db_workspace = TemporaryDirectory()
        cls._template_db_path = Path(cls._db_workspace.name) / "template.db"
        cls._template_database_url = f"sqlite:///{cls._template_db_path}"
        get_settings.cache_clear()
        reset_db_engine_cache()
        run_migrations(database_url=cls._template_database_url)
        reset_db_engine_cache()

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls._db_workspace.cleanup()
        finally:
            get_settings.cache_clear()
            reset_db_engine_cache()
            super().tearDownClass()

    def _prepare_test_database(self, *, name_prefix: str) -> str:
        db_path = Path(self._db_workspace.name) / f"{name_prefix}-{uuid4().hex}.db"
        shutil.copy2(self._template_db_path, db_path)
        self._test_db_path = db_path
        return f"sqlite:///{db_path}"

    def _cleanup_test_database(self) -> None:
        db_path = getattr(self, "_test_db_path", None)
        if isinstance(db_path, Path):
            db_path.unlink(missing_ok=True)


class SqliteTemplateApiTestCase(SqliteTemplateDbTestCase):
    """SQLite template tests with one class-scoped FastAPI app/client."""

    _class_client: TestClient
    _class_env_originals: dict[str, str | None]
    _class_original_database_url: str | None

    @classmethod
    def class_environment_overrides(cls) -> dict[str, str]:
        return {}

    @classmethod
    def bootstrap_template_state(cls) -> None:
        """Optional hook for one-time API/bootstrap work on the template DB."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._class_env_originals = {}
        for key, value in cls.class_environment_overrides().items():
            cls._class_env_originals[key] = os.environ.get(key)
            os.environ[key] = value
        cls._class_original_database_url = os.environ.get("ORCHESTRATOR_DATABASE_URL")
        os.environ["ORCHESTRATOR_DATABASE_URL"] = cls._template_database_url
        get_settings.cache_clear()
        reset_db_engine_cache()
        from orchestrator.api.main import create_app

        cls._class_client = TestClient(create_app())
        cls.bootstrap_template_state()

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls._class_client.close()
        finally:
            if cls._class_original_database_url is None:
                os.environ.pop("ORCHESTRATOR_DATABASE_URL", None)
            else:
                os.environ["ORCHESTRATOR_DATABASE_URL"] = cls._class_original_database_url
            for key, original_value in cls._class_env_originals.items():
                if original_value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = original_value
            get_settings.cache_clear()
            reset_db_engine_cache()
            super().tearDownClass()

    def _start_test_database(self, *, name_prefix: str) -> str:
        database_url = self._prepare_test_database(name_prefix=name_prefix)
        os.environ["ORCHESTRATOR_DATABASE_URL"] = database_url
        get_settings.cache_clear()
        reset_db_engine_cache()
        self.client = self.__class__._class_client
        return database_url
