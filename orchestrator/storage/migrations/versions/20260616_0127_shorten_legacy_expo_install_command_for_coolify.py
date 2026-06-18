"""shorten legacy expo install command for coolify

Revision ID: 20260616_0127
Revises: 20260616_0126
Create Date: 2026-06-16 11:30:00.000000
"""

from __future__ import annotations

from typing import Any

from alembic import op
import sqlalchemy as sa


revision = "20260616_0127"
down_revision = "20260616_0126"
branch_labels = None
depends_on = None

LEGACY_EXPO_CLI_INSTALL_COMMAND_WITH_NATIVE_BUILD_TOOLS_BUT_WITHOUT_PROJECT_DEPS = (
    "sudo apt-get update && sudo apt-get install -y --no-install-recommends python3 make g++ && "
    "npm install --package-lock=false --legacy-peer-deps --production=false --no-save "
    "expo-cli@3.28.6 websocket@1.0.35"
)
OVERSIZED_LEGACY_EXPO_CLI_INSTALL_COMMAND = (
    "sudo apt-get update && sudo apt-get install -y --no-install-recommends python3 make g++ && "
    "npm install --package-lock=false --legacy-peer-deps --production=false && "
    "npm install --package-lock=false --legacy-peer-deps --production=false --no-save "
    "expo-cli@3.28.6 websocket@1.0.35"
)
LEGACY_EXPO_CLI_INSTALL_COMMAND = (
    "sudo apt-get update && sudo apt-get install -y python3 make g++ && "
    "npm install --legacy-peer-deps && "
    "npm install --legacy-peer-deps --no-save expo-cli@3.28.6 websocket@1.0.35"
)
REPAIRABLE_INSTALL_COMMANDS = {
    LEGACY_EXPO_CLI_INSTALL_COMMAND_WITH_NATIVE_BUILD_TOOLS_BUT_WITHOUT_PROJECT_DEPS,
    OVERSIZED_LEGACY_EXPO_CLI_INSTALL_COMMAND,
}


def _table_exists(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table_name in inspector.get_table_names()


def _column_exists(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return False
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def canonical_legacy_expo_install_command(payload: Any) -> tuple[Any, bool]:
    if not isinstance(payload, dict):
        return payload, False
    if payload.get("install_command") not in REPAIRABLE_INSTALL_COMMANDS:
        return payload, False
    updated = dict(payload)
    updated["install_command"] = LEGACY_EXPO_CLI_INSTALL_COMMAND
    return updated, True


def upgrade() -> None:
    if not (_table_exists("project_apps") and _column_exists("project_apps", "deployment_config")):
        return

    bind = op.get_bind()
    project_apps_table = sa.table(
        "project_apps",
        sa.column("app_id", sa.String()),
        sa.column("deployment_config", sa.JSON()),
    )
    rows = bind.execute(sa.text("SELECT app_id, deployment_config FROM project_apps")).mappings().all()
    for row in rows:
        updated, changed = canonical_legacy_expo_install_command(row["deployment_config"])
        if not changed:
            continue
        bind.execute(
            project_apps_table.update()
            .where(project_apps_table.c.app_id == row["app_id"])
            .values(deployment_config=updated)
        )


def downgrade() -> None:
    return None
