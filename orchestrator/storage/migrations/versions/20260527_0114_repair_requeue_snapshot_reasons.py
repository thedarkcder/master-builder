"""repair requeue execution snapshots without reasons

Revision ID: 20260527_0114
Revises: 20260527_0113
Create Date: 2026-05-27 18:05:00.000000
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from alembic import op
import sqlalchemy as sa


revision = "20260527_0114"
down_revision = "20260527_0113"
branch_labels = None
depends_on = None


_DEFAULT_REQUEUE_REASON = "Workflow requested requeue."


def upgrade() -> None:
    bind = op.get_bind()
    metadata = sa.MetaData()
    runs = sa.Table("runs", metadata, autoload_with=bind)
    checkpoints = sa.Table("workflow_checkpoints", metadata, autoload_with=bind)

    for row in bind.execute(sa.select(runs.c.run_id, runs.c.plan).where(runs.c.plan.is_not(None))).mappings():
        repaired = _repair_requeue_snapshot_reason(row.get("plan"))
        if repaired is None:
            continue
        bind.execute(runs.update().where(runs.c.run_id == row["run_id"]).values(plan=repaired))

    now = datetime.now(timezone.utc)
    for row in bind.execute(
        sa.select(checkpoints.c.checkpoint_id, checkpoints.c.payload_json).where(checkpoints.c.payload_json.is_not(None))
    ).mappings():
        repaired = _repair_requeue_snapshot_reason(row.get("payload_json"))
        if repaired is None:
            continue
        bind.execute(
            checkpoints.update()
            .where(checkpoints.c.checkpoint_id == row["checkpoint_id"])
            .values(payload_json=repaired, updated_at=now)
        )


def downgrade() -> None:
    return None


def _repair_requeue_snapshot_reason(payload: object | None) -> dict[str, Any] | None:
    if not isinstance(payload, dict) or payload.get("version") != 1:
        return None
    workflow = payload.get("workflow")
    if not isinstance(workflow, dict) or workflow.get("outcome") != "requeue":
        return None
    reason = workflow.get("requeue_reason")
    if isinstance(reason, str) and reason.strip():
        return None

    repaired = dict(payload)
    repaired_workflow = dict(workflow)
    repaired_workflow["requeue_reason"] = _derive_requeue_reason(workflow)
    repaired["workflow"] = repaired_workflow
    return repaired


def _derive_requeue_reason(workflow: dict[str, Any]) -> str:
    for key in ("blocker_message",):
        value = workflow.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return _DEFAULT_REQUEUE_REASON
