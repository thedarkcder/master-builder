"""expand project deployment policy contract

Revision ID: 20260508_0117
Revises: 20260508_0116
Create Date: 2026-05-08 17:20:00.000000
"""

from __future__ import annotations

import json
from typing import Any

from alembic import op
import sqlalchemy as sa


revision = "20260508_0117"
down_revision = "20260508_0116"
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


def _expanded_policy(current: dict[str, Any]) -> dict[str, Any]:
    enabled = bool(current.get("enabled") is True)
    branch = _normalize_branch(current.get("production_branch"))
    secret_refs = (
        current.get("secret_refs")
        if isinstance(current.get("secret_refs"), dict)
        else {}
    )
    branch_settings: dict[str, Any] = {}
    if branch is not None and secret_refs:
        branch_settings[branch] = {"environment": {}, "secret_refs": secret_refs}
    policy: dict[str, Any] = {
        "enabled": enabled,
        "preview_prs_enabled": False,
        "provider": "internal_coolify",
        "generated_domain_policy": current.get("generated_domain_policy")
        or "production",
        "branch_settings": branch_settings,
        "resources": current.get("resources")
        if isinstance(current.get("resources"), list)
        else [],
    }
    if enabled and branch is not None:
        policy["production_branch"] = branch
    if str(current.get("deployment_host_id") or "").strip():
        policy["deployment_host_id"] = str(current["deployment_host_id"]).strip()
    return policy


def upgrade() -> None:
    bind = op.get_bind()
    if not (
        _table_exists("projects") and _column_exists("projects", "deployment_config")
    ):
        return

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
        bind.execute(
            projects_table.update()
            .where(projects_table.c.project_id == row["project_id"])
            .values(
                deployment_config=_expanded_policy(
                    _coerce_dict(row["deployment_config"])
                )
            )
        )


def downgrade() -> None:
    # Data-only policy expansion; old deployments can still read the explicit fields harmlessly.
    return None
