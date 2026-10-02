"""repair legacy expo native build environment

Revision ID: 20260616_0128
Revises: 20260616_0127
Create Date: 2026-06-16 12:05:00.000000
"""

from __future__ import annotations

from typing import Any

from alembic import op
import sqlalchemy as sa


revision = "20260616_0128"
down_revision = "20260616_0127"
branch_labels = None
depends_on = None

LEGACY_EXPO_CLI_INSTALL_COMMAND = (
    "sudo apt-get update && sudo apt-get install -y python3 make g++ && "
    "npm install --legacy-peer-deps && "
    "npm install --legacy-peer-deps --no-save expo-cli@3.28.6 websocket@1.0.35"
)
LEGACY_EXPO_RELEASE_ENVIRONMENT = {
    "NODE_ENV": "development",
    "NODE_OPTIONS": "--openssl-legacy-provider",
    "NPM_CONFIG_PRODUCTION": "false",
    "NPM_CONFIG_PYTHON": "/usr/bin/python3",
    "PYTHON": "/usr/bin/python3",
}


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


def canonical_legacy_expo_environment(payload: Any) -> tuple[Any, bool]:
    if not isinstance(payload, dict):
        return payload, False
    if payload.get("install_command") != LEGACY_EXPO_CLI_INSTALL_COMMAND:
        return payload, False
    existing_environment = payload.get("environment")
    if existing_environment is not None and not isinstance(existing_environment, dict):
        return payload, False
    environment = dict(existing_environment or {})
    changed = False
    for key, value in LEGACY_EXPO_RELEASE_ENVIRONMENT.items():
        if environment.get(key) == value:
            continue
        environment[key] = value
        changed = True
    if not changed:
        return payload, False
    updated = dict(payload)
    updated["environment"] = environment
    return updated, True


def upgrade() -> None:
    if not (
        _table_exists("project_apps")
        and _column_exists("project_apps", "deployment_config")
    ):
        return

    bind = op.get_bind()
    project_apps_table = sa.table(
        "project_apps",
        sa.column("app_id", sa.String()),
        sa.column("deployment_config", sa.JSON()),
    )
    rows = (
        bind.execute(sa.text("SELECT app_id, deployment_config FROM project_apps"))
        .mappings()
        .all()
    )
    for row in rows:
        updated, changed = canonical_legacy_expo_environment(row["deployment_config"])
        if not changed:
            continue
        bind.execute(
            project_apps_table.update()
            .where(project_apps_table.c.app_id == row["app_id"])
            .values(deployment_config=updated)
        )


def downgrade() -> None:
    return None
