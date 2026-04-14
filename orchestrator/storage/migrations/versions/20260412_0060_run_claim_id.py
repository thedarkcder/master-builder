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


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    run_columns = {column["name"] for column in inspector.get_columns("runs")}
    run_indexes = {index["name"] for index in inspector.get_indexes("runs")}
    with op.batch_alter_table("runs") as batch_op:
        if "claim_id" not in run_columns:
            batch_op.add_column(sa.Column("claim_id", sa.String(length=64), nullable=True))
    if "ix_runs_claim_id" not in run_indexes:
        op.create_index("ix_runs_claim_id", "runs", ["claim_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_runs_claim_id", table_name="runs")
    with op.batch_alter_table("runs") as batch_op:
        batch_op.drop_column("claim_id")
