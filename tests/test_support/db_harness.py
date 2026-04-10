from __future__ import annotations

import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from orchestrator.core.config import get_settings
from orchestrator.storage.db import reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations


class SqliteTemplateDbTestCase(unittest.TestCase):
    """Build one migrated SQLite template per class, then copy per test."""

    _db_workspace: TemporaryDirectory[str]
    _template_db_path: Path

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._db_workspace = TemporaryDirectory()
        cls._template_db_path = Path(cls._db_workspace.name) / "template.db"
        template_database_url = f"sqlite:///{cls._template_db_path}"
        get_settings.cache_clear()
        reset_db_engine_cache()
        run_migrations(database_url=template_database_url)
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
