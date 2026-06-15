"""remove qa capture metadata from project app deployment config

Revision ID: 20260615_0119
Revises: 20260601_0118
Create Date: 2026-06-15 12:00:00.000000
"""

from __future__ import annotations

import json
from typing import Any

from alembic import op
import sqlalchemy as sa


revision = "20260615_0119"
down_revision = "20260601_0118"
branch_labels = None
depends_on = None

_QA_CAPTURE_METADATA_KEYS = frozenset({"capture_target", "mobile_platform"})


def _table_exists(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table_name in inspector.get_table_names()


def _column_exists(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return False
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def _coerce_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        decoded = json.loads(value)
        return dict(decoded) if isinstance(decoded, dict) else {}
    return {}


def strip_qa_capture_metadata(value: Any) -> tuple[dict[str, Any], bool]:
    current = _coerce_dict(value)
    updated = {key: item for key, item in current.items() if key not in _QA_CAPTURE_METADATA_KEYS}
    return updated, updated != current


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
        updated, changed = strip_qa_capture_metadata(row["deployment_config"])
        if not changed:
            continue
        bind.execute(
            project_apps_table.update()
            .where(project_apps_table.c.app_id == row["app_id"])
            .values(deployment_config=updated)
        )


def downgrade() -> None:
    return None
