"""split project deployment policy from app deployment config

Revision ID: 20260508_0116
Revises: 20260508_0115
Create Date: 2026-05-08 16:10:00.000000
"""

from __future__ import annotations

import json
from typing import Any

from alembic import op
import sqlalchemy as sa


revision = "20260508_0116"
down_revision = "20260508_0115"
branch_labels = None
depends_on = None


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


def _coerce_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        decoded = json.loads(value)
        return dict(decoded) if isinstance(decoded, dict) else {}
    return {}


def _normalize_branch(value: Any) -> str | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    for prefix in ("refs/heads/", "origin/"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :].strip()
            break
    return normalized or None


def upgrade() -> None:
    bind = op.get_bind()
    if _table_exists("projects") and _column_exists("projects", "deployment_config"):
        projects_table = sa.table(
            "projects",
            sa.column("project_id", sa.String()),
            sa.column("deployment_config", sa.JSON()),
        )
        rows = (
            bind.execute(sa.text("SELECT project_id, deployment_config FROM projects"))
            .mappings()
            .all()
        )
        for row in rows:
            current = _coerce_dict(row["deployment_config"])
            policy: dict[str, Any] = {}
            if current.get("auto_deploy_enabled") is True:
                policy["enabled"] = True
                branch = _normalize_branch(current.get("production_branch"))
                if branch is not None:
                    policy["production_branch"] = branch
            elif (
                current.get("enabled") is True
                and _normalize_branch(current.get("production_branch")) is not None
            ):
                policy["enabled"] = True
                policy["production_branch"] = _normalize_branch(
                    current.get("production_branch")
                )
            bind.execute(
                projects_table.update()
                .where(projects_table.c.project_id == row["project_id"])
                .values(deployment_config=policy)
            )

    if _table_exists("project_apps") and _column_exists(
        "project_apps", "deployment_config"
    ):
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
            current = _coerce_dict(row["deployment_config"])
            if (
                "auto_deploy_enabled" not in current
                and "production_branch" not in current
            ):
                continue
            current.pop("auto_deploy_enabled", None)
            current.pop("production_branch", None)
            bind.execute(
                project_apps_table.update()
                .where(project_apps_table.c.app_id == row["app_id"])
                .values(deployment_config=current)
            )


def downgrade() -> None:
    # Data-only contract split; app deployment config policy fields are intentionally not restored.
    return None
