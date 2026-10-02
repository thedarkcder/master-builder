from __future__ import annotations

import os
import secrets
import shutil
from collections.abc import Iterator, Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pytest

_test_defaults_workspace = TemporaryDirectory(prefix="master-builder-pytest-")
_test_template_path = Path(_test_defaults_workspace.name) / "template.db"
_test_default_database_url = f"sqlite:///{_test_template_path}"
_test_default_runtime_home = str(Path(_test_defaults_workspace.name) / "runtime")

# Generic tests use this explicit local contract, never a developer's database
# or runtime directory. Database harnesses can select their own test resources.
os.environ["ORCHESTRATOR_DATABASE_URL"] = _test_default_database_url
os.environ["ORCHESTRATOR_ALLOW_SQLITE_FOR_TESTS"] = "true"
os.environ["ORCHESTRATOR_RUNTIME_HOME"] = _test_default_runtime_home
os.environ["ORCHESTRATOR_TRUSTED_HOSTS"] = "testserver,localhost,127.0.0.1"

# Tests configure authentication explicitly before modules construct the API app.
# These process-local values are never credentials for a development deployment.
for _secret_name in (
    "ADMIN_PASSWORD",
    "ADMIN_TOKEN_SECRET",
    "AUTH_TOKEN_SECRET",
    "DISCORD_INSTALL_STATE_SECRET",
    "JIRA_ACTION_TOKEN_SECRET",
    "GITHUB_INSTALL_STATE_SECRET",
    "ATLASSIAN_OAUTH_STATE_SECRET",
):
    os.environ.setdefault(f"ORCHESTRATOR_{_secret_name}", secrets.token_urlsafe(32))


@pytest.fixture(scope="session", autouse=True)
def _migrated_test_defaults() -> Iterator[None]:
    from orchestrator.core.config import get_settings
    from orchestrator.storage.db import reset_db_engine_cache
    from orchestrator.storage.migrations import run_migrations

    get_settings.cache_clear()
    run_migrations(database_url=_test_default_database_url)
    yield
    get_settings.cache_clear()
    reset_db_engine_cache()
    _test_defaults_workspace.cleanup()


@pytest.fixture(autouse=True)
def _isolate_test_environment(
    tmp_path: Path, _migrated_test_defaults: None
) -> Iterator[None]:
    """Restore process env after every test to prevent cross-test leakage."""
    before = dict(os.environ)
    from orchestrator.core.config import get_settings
    from orchestrator.storage.db import create_db_engine

    test_database_path = None
    test_runtime_home = None
    test_engine = None
    if os.environ.get("ORCHESTRATOR_DATABASE_URL") == _test_default_database_url:
        test_database_path = tmp_path / "database.db"
        shutil.copy2(_test_template_path, test_database_path)
        os.environ["ORCHESTRATOR_DATABASE_URL"] = f"sqlite:///{test_database_path}"
    if os.environ.get("ORCHESTRATOR_RUNTIME_HOME") == _test_default_runtime_home:
        test_runtime_home = tmp_path / "runtime"
        test_runtime_home.mkdir(mode=0o700)
        os.environ["ORCHESTRATOR_RUNTIME_HOME"] = str(test_runtime_home)
    get_settings.cache_clear()
    if test_database_path is not None:
        test_engine = create_db_engine(database_url=f"sqlite:///{test_database_path}")
    yield

    after_keys = set(os.environ)
    before_keys = set(before)
    for key in after_keys - before_keys:
        os.environ.pop(key, None)
    for key in before_keys:
        current = os.environ.get(key)
        expected = before[key]
        if current != expected:
            os.environ[key] = expected

    # Ensure settings reads in the next test reflect restored env values.
    get_settings.cache_clear()
    if test_engine is not None:
        test_engine.dispose()
        test_database_path.unlink(missing_ok=True)
    if test_runtime_home is not None:
        shutil.rmtree(test_runtime_home)


@pytest.fixture
def monkeypatch(
    _isolate_test_environment: None,
) -> Iterator[pytest.MonkeyPatch]:
    """Undo patches before restoring environment and deleting test resources."""
    with pytest.MonkeyPatch.context() as patch:
        yield patch


@pytest.fixture(autouse=True)
def _isolate_legacy_runtime_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep implicit legacy reads outside contributors' actual Codex state.

    Tests may still explicitly select their own synthetic HOME/repository
    snapshots. The production migration and process-wide HOME are untouched.
    """
    from orchestrator.core.runtime import runtime_home

    original_home = os.environ.get("HOME")
    owned_home = tmp_path / "legacy-home"
    owned_home.mkdir(mode=0o700)
    checkout = Path(__file__).resolve().parents[1]
    original_repo_source = runtime_home._legacy_repo_runtime_home

    class RuntimeEnvironment(Mapping[str, str]):
        def __getitem__(self, key: str) -> str:
            if key == "HOME" and os.environ.get("HOME") == original_home:
                return str(owned_home)
            return os.environ[key]

        def __iter__(self) -> Iterator[str]:
            return iter(set(os.environ) | {"HOME"})

        def __len__(self) -> int:
            return len(set(os.environ) | {"HOME"})

    def owned_repo_source(*, runtime_kind: str) -> Path:
        if runtime_home._repo_root() == checkout:
            return tmp_path / "legacy-repository" / ".runtime-home" / runtime_kind
        return original_repo_source(runtime_kind=runtime_kind)

    monkeypatch.setattr(
        runtime_home, "os", SimpleNamespace(environ=RuntimeEnvironment())
    )
    monkeypatch.setattr(runtime_home, "_legacy_repo_runtime_home", owned_repo_source)


@pytest.fixture(autouse=True)
def _isolate_product_event_store(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Unit tests use explicit fakes for ClickHouse-backed product events."""
    from orchestrator.core.observability.repository import (
        configure_product_event_repository_for_tests,
        reset_product_event_repository_for_tests,
    )

    class _FakeProductEventRepository:
        def initialize(self) -> None:
            return None

        def insert_event(self, row) -> None:  # noqa: ANN001
            return None

        def list_events(self, **_kwargs):  # noqa: ANN003
            return []

        def list_events_after_sequence(self, **_kwargs):  # noqa: ANN003
            return []

    configure_product_event_repository_for_tests(_FakeProductEventRepository())
    monkeypatch.setattr(
        "orchestrator.core.observability.writer.publish_product_event_notification",
        lambda **_kwargs: None,
    )
    yield
    reset_product_event_repository_for_tests()
