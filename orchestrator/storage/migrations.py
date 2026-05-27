from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
import sqlalchemy as sa

from orchestrator.core.config import get_settings
from orchestrator.storage.database_support import ensure_postgres_database_url


_TOP_REVISION_IDS = {
    "20260327_0039",
    "20260327_0040",
    "20260327_0041",
}

_ORPHANED_REVISION_REPAIRS = {
    "20260509_0122": "20260523_0107",
}


def _table_exists(inspector: sa.Inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def _column_exists(inspector: sa.Inspector, table_name: str, column_name: str) -> bool:
    if not _table_exists(inspector, table_name):
        return False
    return any(column.get("name") == column_name for column in inspector.get_columns(table_name))


def _review_id_is_bigint(inspector: sa.Inspector) -> bool:
    if not _table_exists(inspector, "pr_review_publications"):
        return False
    for column in inspector.get_columns("pr_review_publications"):
        if column.get("name") != "review_id":
            continue
        column_type = column.get("type")
        return isinstance(column_type, sa.BigInteger) or str(column_type).upper() == "BIGINT"
    return False


def _index_columns(inspector: sa.Inspector, table_name: str, index_name: str) -> list[str] | None:
    if not _table_exists(inspector, table_name):
        return None
    for index in inspector.get_indexes(table_name):
        if index.get("name") == index_name:
            return list(index.get("column_names") or [])
    return None


def _desired_top_revisions(inspector: sa.Inspector) -> list[str] | None:
    tenant_revision: str | None = None
    if _column_exists(inspector, "tenant_memberships", "discord_state") or _table_exists(
        inspector, "tenant_user_discord_identities"
    ):
        tenant_revision = "20260327_0040"
    elif _column_exists(inspector, "tenants", "experience_config") or _table_exists(inspector, "tenant_users"):
        tenant_revision = "20260327_0039"

    review_revision = "20260327_0041" if _review_id_is_bigint(inspector) else None
    desired = {revision for revision in (tenant_revision, review_revision) if revision is not None}
    if not desired:
        return None
    return sorted(desired)


def _normalize_repaired_top_revisions(database_url: str) -> None:
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            inspector = inspect(connection)
            if not _table_exists(inspector, "alembic_version"):
                return
            current_rows = [
                str(value)
                for value in connection.execute(text("SELECT version_num FROM alembic_version ORDER BY version_num")).scalars()
            ]
            current = set(current_rows)
            if not current or not current.issubset(_TOP_REVISION_IDS):
                return
            if current.issubset({"20260327_0039", "20260327_0040"}) and not (
                _table_exists(inspector, "tenant_users")
                or _table_exists(inspector, "tenant_user_discord_identities")
                or _column_exists(inspector, "tenant_memberships", "discord_state")
            ):
                connection.execute(text("DELETE FROM alembic_version"))
                connection.execute(text("INSERT INTO alembic_version (version_num) VALUES ('20260323_0038')"))
                return
            desired_rows = _desired_top_revisions(inspector)
            if desired_rows is None or current_rows == desired_rows:
                return
            connection.execute(text("DELETE FROM alembic_version"))
            for revision in desired_rows:
                connection.execute(
                    text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
                    {"revision": revision},
                )
    finally:
        engine.dispose()


def _repair_stamp_if_schema_ahead_of_version(database_url: str) -> None:
    """Align alembic_version when DDL from a migration is present but the stamp lags.

    Deploys can end up with schema effects from ``20260328_0045`` / ``20260328_0046``
    while ``alembic_version`` still points at the parent revision (manual DDL, partial
    recovery, or a race). Alembic then fails updating ``20260328_0045`` → ``20260328_0046``
    because no row matches ``version_num = '20260328_0045'``.
    """
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            inspector = inspect(connection)
            if not _table_exists(inspector, "alembic_version"):
                return
            rows = [
                str(value)
                for value in connection.execute(
                    text("SELECT version_num FROM alembic_version ORDER BY version_num")
                ).scalars()
            ]
            if len(rows) != 1:
                return
            current = rows[0]
            if current == "20260328_0044" and _table_exists(inspector, "worker_runtime_states"):
                connection.execute(text("UPDATE alembic_version SET version_num = '20260328_0045'"))
                current = "20260328_0045"
            if current == "20260328_0045" and _table_exists(inspector, "platform_settings"):
                connection.execute(text("UPDATE alembic_version SET version_num = '20260328_0046'"))
    finally:
        engine.dispose()


def _repair_revision_is_materialized(inspector: sa.Inspector, repair_revision: str) -> bool:
    if repair_revision != "20260523_0107":
        raise RuntimeError(
            f"Orphaned alembic revision repair is not configured for repair target {repair_revision}; "
            "add an explicit schema contract before repairing stale stamps."
        )
    return (
        _table_exists(inspector, "planning_decision_records")
        and _table_exists(inspector, "workflow_operation_work_units")
        and _column_exists(inspector, "workflow_executions", "source_external_id")
        and _column_exists(inspector, "workflow_operation_attempts", "last_heartbeat_at")
        and _column_exists(inspector, "workflow_operation_attempts", "lease_expires_at")
        and _column_exists(inspector, "workflow_operation_attempts", "lease_owner")
        and _table_exists(inspector, "workflow_executable_work_items")
        and _index_columns(
            inspector,
            "workflow_executions",
            "ix_workflow_executions_source_external_id",
        )
        == ["tenant_id", "source_system", "source_external_id", "dedupe_scope"]
    )


def _repair_orphaned_revision_stamp(database_url: str, head_revision: str) -> None:
    _ = head_revision
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            inspector = inspect(connection)
            if not _table_exists(inspector, "alembic_version"):
                return
            rows = [
                str(value)
                for value in connection.execute(
                    text("SELECT version_num FROM alembic_version ORDER BY version_num")
                ).scalars()
            ]
            if len(rows) != 1:
                return
            current_revision = rows[0]
            expected_head = _ORPHANED_REVISION_REPAIRS.get(current_revision)
            if expected_head is None:
                return
            if not _repair_revision_is_materialized(inspector, expected_head):
                raise RuntimeError(
                    "Database is stamped to orphaned Alembic revision "
                    f"{current_revision}, but the live schema does not satisfy repair target {expected_head}. "
                    "Manual migration intervention is required."
                )
            connection.execute(
                text("UPDATE alembic_version SET version_num = :head_revision"),
                {"head_revision": expected_head},
            )
    finally:
        engine.dispose()


def run_migrations(database_url: str | None = None) -> None:
    settings = get_settings()
    root = Path(__file__).resolve().parents[2]
    target_database_url = database_url or settings.database_url
    ensure_postgres_database_url(
        database_url=target_database_url,
        context="Migrations",
        allow_sqlite_for_tests=bool(getattr(settings, "allow_sqlite_for_tests", False)),
    )

    config = Config(str(root / "alembic.ini"))
    # Keep application logging configuration intact; Alembic's default fileConfig
    # would otherwise reset handlers/levels (which hides request logs).
    config.attributes["configure_logger"] = False
    config.set_main_option(
        "script_location",
        str(root / "orchestrator" / "storage" / "migrations"),
    )
    config.set_main_option("sqlalchemy.url", target_database_url)
    head_revisions = ScriptDirectory.from_config(config).get_heads()
    if len(head_revisions) != 1:
        raise RuntimeError(
            f"Expected a single Alembic head before running migrations, found {head_revisions!r}."
        )
    _normalize_repaired_top_revisions(target_database_url)
    _repair_stamp_if_schema_ahead_of_version(target_database_url)
    _repair_orphaned_revision_stamp(target_database_url, head_revisions[0])
    command.upgrade(config, "head")
