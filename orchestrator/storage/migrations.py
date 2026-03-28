from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
import sqlalchemy as sa

from orchestrator.core.config import get_settings


_TOP_REVISION_IDS = {
    "20260327_0039",
    "20260327_0040",
    "20260327_0041",
    "20260327_0042",
    "20260327_0043",
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


def _desired_top_revisions(inspector: sa.Inspector) -> list[str] | None:
    tenant_revision: str | None = None
    if _column_exists(inspector, "tenant_memberships", "discord_state") or _table_exists(
        inspector, "tenant_user_discord_identities"
    ):
        tenant_revision = "20260327_0040"
    elif _column_exists(inspector, "tenants", "experience_config") or _table_exists(inspector, "tenant_users"):
        tenant_revision = "20260327_0039"

    stream_revision: str | None = None
    if _table_exists(inspector, "run_stream_events"):
        stream_revision = "20260327_0042"
    elif _review_id_is_bigint(inspector):
        stream_revision = "20260327_0041"

    desired = {revision for revision in (tenant_revision, stream_revision) if revision is not None}
    if not desired:
        return None
    if desired == {"20260327_0040", "20260327_0042"}:
        return ["20260327_0043"]
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


def run_migrations(database_url: str | None = None) -> None:
    settings = get_settings()
    root = Path(__file__).resolve().parents[2]
    target_database_url = database_url or settings.database_url

    _normalize_repaired_top_revisions(target_database_url)

    config = Config(str(root / "alembic.ini"))
    # Keep application logging configuration intact; Alembic's default fileConfig
    # would otherwise reset handlers/levels (which hides request logs).
    config.attributes["configure_logger"] = False
    config.set_main_option(
        "script_location",
        str(root / "orchestrator" / "storage" / "migrations"),
    )
    config.set_main_option("sqlalchemy.url", target_database_url)
    command.upgrade(config, "head")
