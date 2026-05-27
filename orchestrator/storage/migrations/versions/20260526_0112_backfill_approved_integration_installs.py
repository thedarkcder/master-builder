"""Backfill approved integration install records.

Revision ID: 20260526_0112
Revises: 20260526_0111
Create Date: 2026-05-26 18:40:00.000000
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from uuid import uuid4

from alembic import op
import sqlalchemy as sa


revision = "20260526_0112"
down_revision = "20260526_0111"
branch_labels = None
depends_on = None


def _json_object(value: object) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    return {}


def _json_list(value: object) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return parsed
    return []


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("project_install_requests") or not inspector.has_table("project_installs"):
        return

    timestamp = datetime.now(timezone.utc)
    rows = list(
        bind.execute(
            sa.text(
                """
                SELECT request_id,
                       tenant_id,
                       project_id,
                       kind,
                       label,
                       suggested_config_json,
                       required_bindings_json
                FROM project_install_requests
                WHERE status = 'approved'
                  AND kind = 'integration'
                """
            )
        ).mappings()
    )
    project_installs = sa.table(
        "project_installs",
        sa.column("install_id", sa.String()),
        sa.column("tenant_id", sa.String()),
        sa.column("project_id", sa.String()),
        sa.column("kind", sa.String()),
        sa.column("label", sa.String()),
        sa.column("enabled", sa.Boolean()),
        sa.column("config_json", sa.JSON()),
        sa.column("binding_names_json", sa.JSON()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    for row in rows:
        exists = bind.execute(
            sa.text(
                """
                SELECT 1
                FROM project_installs
                WHERE tenant_id = :tenant_id
                  AND project_id = :project_id
                  AND kind = :kind
                  AND label = :label
                LIMIT 1
                """
            ),
            {
                "tenant_id": row["tenant_id"],
                "project_id": row["project_id"],
                "kind": row["kind"],
                "label": row["label"],
            },
        ).first()
        if exists is not None:
            continue
        bind.execute(
            project_installs.insert().values(
                install_id=uuid4().hex,
                tenant_id=row["tenant_id"],
                project_id=row["project_id"],
                kind=row["kind"],
                label=row["label"],
                enabled=True,
                config_json=_json_object(row["suggested_config_json"]),
                binding_names_json=_json_list(row["required_bindings_json"]),
                created_at=timestamp,
                updated_at=timestamp,
            )
        )


def downgrade() -> None:
    raise RuntimeError("Approved integration install backfill cannot be downgraded")
