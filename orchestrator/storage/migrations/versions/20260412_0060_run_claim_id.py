"""add explicit run claim id for dispatch ownership

Revision ID: 20260412_0060
Revises: 20260412_0059
Create Date: 2026-04-12 17:15:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260412_0060"
down_revision = "20260412_0059"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(column.get("name") == column_name for column in inspector.get_columns(table_name))


def _has_index(table_name: str, index_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(index.get("name") == index_name for index in inspector.get_indexes(table_name))


def upgrade() -> None:
    with op.batch_alter_table("runs") as batch_op:
        if not _has_column("runs", "claim_id"):
            batch_op.add_column(sa.Column("claim_id", sa.String(length=64), nullable=True))
    if not _has_index("runs", "ix_runs_claim_id"):
        op.create_index("ix_runs_claim_id", "runs", ["claim_id"], unique=False)


def downgrade() -> None:
    if _has_index("runs", "ix_runs_claim_id"):
        op.drop_index("ix_runs_claim_id", table_name="runs")
    with op.batch_alter_table("runs") as batch_op:
        if _has_column("runs", "claim_id"):
            batch_op.drop_column("claim_id")
