"""persist run queue contract fields and backfill from snapshot plans

Revision ID: 20260412_0059
Revises: 20260410_0058
Create Date: 2026-04-12 15:30:00.000000
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from alembic import op


revision = "20260412_0059"
down_revision = "20260410_0058"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(column.get("name") == column_name for column in inspector.get_columns(table_name))


def _has_index(table_name: str, index_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(index.get("name") == index_name for index in inspector.get_indexes(table_name))


def upgrade() -> None:
    bind = op.get_bind()
    metadata = sa.MetaData()

    with op.batch_alter_table("runs") as batch_op:
        if not _has_column("runs", "pre_check_outcome"):
            batch_op.add_column(sa.Column("pre_check_outcome", sa.String(length=32), nullable=True))
        if not _has_column("runs", "required_worker_capability"):
            batch_op.add_column(sa.Column("required_worker_capability", sa.String(length=32), nullable=True))
        if not _has_column("runs", "dispatch_claimed_at"):
            batch_op.add_column(sa.Column("dispatch_claimed_at", sa.DateTime(timezone=True), nullable=True))

    if not _has_index("runs", "ix_runs_pre_check_outcome"):
        op.create_index("ix_runs_pre_check_outcome", "runs", ["pre_check_outcome"], unique=False)
    if not _has_index("runs", "ix_runs_required_worker_capability"):
        op.create_index("ix_runs_required_worker_capability", "runs", ["required_worker_capability"], unique=False)
    if not _has_index("runs", "ix_runs_dispatch_claimed_at"):
        op.create_index("ix_runs_dispatch_claimed_at", "runs", ["dispatch_claimed_at"], unique=False)
    runs = sa.Table("runs", metadata, autoload_with=bind)

    rows = bind.execute(
        sa.select(runs.c.run_id, runs.c.plan)
        .where(runs.c.plan.is_not(None))
        .order_by(runs.c.created_at.asc(), runs.c.run_id.asc())
    ).mappings()
    for row in rows:
        run_id = str(row.get("run_id") or "").strip()
        if not run_id:
            continue
        payload = row.get("plan")
        if not isinstance(payload, dict):
            continue
        execution_context = _coerce_dict(_coerce_dict(payload.get("context")).get("execution_context"))
        workflow = _coerce_dict(payload.get("workflow"))
        pre_check_outcome = _coerce_string(execution_context.get("pre_check_outcome"))
        required_worker_capability = _coerce_capability(workflow.get("requeue_target"))
        if pre_check_outcome is None and required_worker_capability is None:
            continue
        bind.execute(
            runs.update()
            .where(runs.c.run_id == run_id)
            .values(
                pre_check_outcome=pre_check_outcome,
                required_worker_capability=required_worker_capability,
            )
        )


def downgrade() -> None:
    if _has_index("runs", "ix_runs_dispatch_claimed_at"):
        op.drop_index("ix_runs_dispatch_claimed_at", table_name="runs")
    if _has_index("runs", "ix_runs_required_worker_capability"):
        op.drop_index("ix_runs_required_worker_capability", table_name="runs")
    if _has_index("runs", "ix_runs_pre_check_outcome"):
        op.drop_index("ix_runs_pre_check_outcome", table_name="runs")
    with op.batch_alter_table("runs") as batch_op:
        if _has_column("runs", "dispatch_claimed_at"):
            batch_op.drop_column("dispatch_claimed_at")
        if _has_column("runs", "required_worker_capability"):
            batch_op.drop_column("required_worker_capability")
        if _has_column("runs", "pre_check_outcome"):
            batch_op.drop_column("pre_check_outcome")


def _coerce_dict(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return dict(value)


def _coerce_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _coerce_capability(value: object) -> str | None:
    normalized = _coerce_string(value)
    if normalized in {"linux", "macos"}:
        return normalized
    return None
