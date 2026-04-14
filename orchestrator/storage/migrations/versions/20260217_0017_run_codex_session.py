"""add codex session id to runs

Revision ID: 20260217_0017
Revises: 20260215_0016
Create Date: 2026-02-17 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260217_0017"
down_revision = "20260215_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("runs", recreate="auto") as batch_op:
        batch_op.add_column(sa.Column("codex_session_id", sa.String(length=64), nullable=True))

    op.create_index("ix_runs_codex_session_id", "runs", ["codex_session_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_runs_codex_session_id", table_name="runs")

    with op.batch_alter_table("runs", recreate="auto") as batch_op:
        batch_op.drop_column("codex_session_id")
