from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_codex_runtime_repository_root_resolves_checkout_assets() -> None:
    from orchestrator.core.runtime.runtime_home import _repo_root

    assert _repo_root() == ROOT
    assert (_repo_root() / ".codex" / "POLICY.md").is_file()


def test_source_layout_missing_operational_assets_fails_with_installation_guidance(
    tmp_path: Path,
) -> None:
    from orchestrator.core.source_layout import require_source_checkout

    with pytest.raises(RuntimeError, match="full source checkout") as error:
        require_source_checkout(root=tmp_path)
    assert "standalone wheel" in str(error.value)
    assert "alembic.ini" in str(error.value)


def test_source_layout_accepts_complete_current_checkout() -> None:
    from orchestrator.core.source_layout import require_source_checkout

    assert require_source_checkout() == ROOT


def test_cli_missing_source_assets_fails_before_command_runtime_import() -> None:
    from orchestrator.cli import main

    with (
        patch(
            "orchestrator.cli.require_source_checkout",
            side_effect=RuntimeError("Use a full source checkout"),
        ),
        patch(
            "orchestrator.core.deployment_runtime.run_deployment_reconciler"
        ) as runtime,
    ):
        with pytest.raises(SystemExit) as error:
            main(["deployment-reconciler"])
    assert error.value.code == 2
    runtime.assert_not_called()


def test_cli_help_does_not_validate_or_require_source_assets() -> None:
    from orchestrator.cli import main

    with patch("orchestrator.cli.require_source_checkout") as validate:
        with pytest.raises(SystemExit) as error:
            main(["--help"])
    assert error.value.code == 0
    validate.assert_not_called()


def test_api_lifespan_missing_source_assets_fails_before_database_access() -> None:
    import asyncio
    from orchestrator.api.main import create_app

    app = create_app()

    async def start() -> None:
        async with app.router.lifespan_context(app):
            raise AssertionError("Incomplete runtime must never enter serving state")

    with (
        patch(
            "orchestrator.api.main.require_source_checkout",
            side_effect=RuntimeError("Use a full source checkout"),
        ),
        patch("orchestrator.api.main.create_session_factory") as sessions,
    ):
        with pytest.raises(RuntimeError, match="full source checkout"):
            asyncio.run(start())
    sessions.assert_not_called()


def test_migrations_missing_source_assets_fail_before_database_connections() -> None:
    from orchestrator.storage.migrations import run_migrations

    with (
        patch(
            "orchestrator.storage.migrations.require_source_checkout",
            side_effect=RuntimeError("Use a full source checkout"),
        ),
        patch("orchestrator.storage.migrations.create_engine") as engine,
    ):
        with pytest.raises(RuntimeError, match="full source checkout"):
            run_migrations(database_url="sqlite://")
    engine.assert_not_called()


def test_missing_codex_asset_directory_fails_before_runtime_state_creation(
    tmp_path: Path,
) -> None:
    from types import SimpleNamespace
    from orchestrator.core.runtime.runtime_home import prepare_runtime_home

    state = tmp_path / "state"
    settings = SimpleNamespace(runtime_home=str(state), agent_id="example")
    with patch(
        "orchestrator.core.runtime.runtime_home._repo_codex_dir",
        return_value=tmp_path / "missing",
    ):
        with pytest.raises(
            RuntimeError, match="Required .codex runtime assets are missing"
        ):
            prepare_runtime_home(settings=settings, runtime_kind="codex_cli")
    assert not state.exists()


def test_explicit_empty_migration_url_never_falls_back_to_configured_database() -> None:
    from types import SimpleNamespace
    from orchestrator.storage.migrations import run_migrations

    settings = SimpleNamespace(database_url="sqlite://", allow_sqlite_for_tests=True)
    with (
        patch("orchestrator.storage.migrations.get_settings", return_value=settings),
        patch(
            "orchestrator.storage.migrations.create_engine",
            side_effect=AssertionError(
                "No database access for an invalid explicit URL"
            ),
        ) as engine,
    ):
        with pytest.raises(RuntimeError, match="Migrations requires PostgreSQL"):
            run_migrations(database_url="")
    engine.assert_not_called()
