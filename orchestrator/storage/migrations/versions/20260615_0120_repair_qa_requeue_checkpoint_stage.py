"""repair qa requeue checkpoint stage

Revision ID: 20260615_0120
Revises: 20260615_0119
Create Date: 2026-06-15 14:05:00.000000
"""

from __future__ import annotations

import json
from typing import Any

from alembic import op
import sqlalchemy as sa


revision = "20260615_0120"
down_revision = "20260615_0119"
branch_labels = None
depends_on = None


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


def checkpoint_stage_from_snapshot(value: Any, current_stage: str | None) -> tuple[str | None, bool]:
    payload = _coerce_dict(value)
    stages = payload.get("stages")
    if not isinstance(stages, dict) or not isinstance(stages.get("qa"), dict):
        return current_stage, False
    normalized_current = str(current_stage or "").strip().lower() or None
    return "qa", normalized_current != "qa"


def upgrade() -> None:
    required_columns = ("checkpoint_id", "checkpoint_kind", "stage", "payload_json")
    if not _table_exists("workflow_checkpoints") or not all(
        _column_exists("workflow_checkpoints", column_name) for column_name in required_columns
    ):
        return

    bind = op.get_bind()
    checkpoints_table = sa.table(
        "workflow_checkpoints",
        sa.column("checkpoint_id", sa.String()),
        sa.column("stage", sa.String()),
    )
    rows = bind.execute(
        sa.text(
            """
            SELECT checkpoint_id, checkpoint_kind, stage, payload_json
            FROM workflow_checkpoints
            WHERE checkpoint_kind = 'execution'
            """
        )
    ).mappings().all()
    for row in rows:
        updated_stage, changed = checkpoint_stage_from_snapshot(row["payload_json"], row["stage"])
        if not changed or updated_stage is None:
            continue
        bind.execute(
            checkpoints_table.update()
            .where(checkpoints_table.c.checkpoint_id == row["checkpoint_id"])
            .values(stage=updated_stage)
        )


def downgrade() -> None:
    return None
