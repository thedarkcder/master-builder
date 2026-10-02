from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.engine import make_url

from orchestrator.core.config import get_settings
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import Tenant


def test_generic_tests_use_disposable_migrated_sqlite(tmp_path: Path) -> None:
    settings = get_settings()
    url = make_url(settings.database_url)
    assert url.get_backend_name() == "sqlite"
    assert settings.allow_sqlite_for_tests is True
    assert Path(url.database).is_relative_to(tmp_path)
    with create_session_factory()() as session:
        assert session.execute(select(Tenant)).scalars().all() == []


def test_generic_runtime_home_is_private_and_disposable(tmp_path: Path) -> None:
    runtime_home = Path(get_settings().runtime_home)
    assert runtime_home.is_relative_to(tmp_path)
    assert runtime_home.is_dir()
    assert runtime_home.stat().st_mode & 0o777 == 0o700


def test_generic_fixture_removes_its_database_and_runtime_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.conftest import (
        _isolate_test_environment,
        _test_default_database_url,
        _test_default_runtime_home,
    )

    monkeypatch.setenv("ORCHESTRATOR_DATABASE_URL", _test_default_database_url)
    monkeypatch.setenv("ORCHESTRATOR_RUNTIME_HOME", _test_default_runtime_home)
    nested_path = tmp_path / "nested-test"
    nested_path.mkdir()
    lifecycle = _isolate_test_environment.__wrapped__(nested_path, None)
    next(lifecycle)
    database = Path(make_url(get_settings().database_url).database)
    runtime_home = Path(get_settings().runtime_home)
    assert database.is_file()
    assert runtime_home.is_dir()
    with pytest.raises(StopIteration):
        next(lifecycle)
    assert not database.exists()
    assert not runtime_home.exists()
