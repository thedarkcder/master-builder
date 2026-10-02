"""repair invalid legacy expo web host

Revision ID: 20260615_0122
Revises: 20260615_0121
Create Date: 2026-06-15 18:55:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260615_0122"
down_revision = "20260615_0121"
branch_labels = None
depends_on = None

LEGACY_EXPO_WEB_START_COMMAND = "npx expo-cli start --web --non-interactive --host lan"
INVALID_LEGACY_EXPO_WEB_START_COMMAND = (
    "npx expo-cli start --web --non-interactive --host 0.0.0.0"
)


def _table_exists(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table_name in inspector.get_table_names()


def _column_exists(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return False
    return any(
        column["name"] == column_name for column in inspector.get_columns(table_name)
    )


def canonical_legacy_expo_web_host(
    *, detected_runtime: str | None, start_command: str | None
) -> tuple[str | None, bool]:
    if (
        detected_runtime != "react_native_web"
        or start_command != INVALID_LEGACY_EXPO_WEB_START_COMMAND
    ):
        return start_command, False
    return LEGACY_EXPO_WEB_START_COMMAND, True


def upgrade() -> None:
    required_columns = ("app_id", "detected_runtime", "start_command")
    if not _table_exists("project_apps") or not all(
        _column_exists("project_apps", column_name) for column_name in required_columns
    ):
        return

    bind = op.get_bind()
    project_apps_table = sa.table(
        "project_apps",
        sa.column("app_id", sa.String()),
        sa.column("start_command", sa.Text()),
    )
    rows = (
        bind.execute(
            sa.text(
                """
            SELECT app_id, detected_runtime, start_command
            FROM project_apps
            WHERE detected_runtime = 'react_native_web'
              AND start_command = :invalid_start_command
            """
            ),
            {"invalid_start_command": INVALID_LEGACY_EXPO_WEB_START_COMMAND},
        )
        .mappings()
        .all()
    )
    for row in rows:
        updated_start_command, changed = canonical_legacy_expo_web_host(
            detected_runtime=row["detected_runtime"],
            start_command=row["start_command"],
        )
        if not changed:
            continue
        bind.execute(
            project_apps_table.update()
            .where(project_apps_table.c.app_id == row["app_id"])
            .values(start_command=updated_start_command)
        )


def downgrade() -> None:
    return None
