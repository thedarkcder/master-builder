"""unify codex log events across all invocation channels

Revision ID: 20260215_0016
Revises: 20260214_0015
Create Date: 2026-02-15 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260215_0016"
down_revision = "20260214_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("run_log_events", recreate="auto") as batch_op:
        batch_op.add_column(sa.Column("invocation_id", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("channel", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("command", sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column("working_dir", sa.String(length=1024), nullable=True))
        batch_op.alter_column("run_id", existing_type=sa.String(length=64), nullable=True)

    op.create_index("ix_run_log_events_invocation_id", "run_log_events", ["invocation_id"], unique=False)
    op.create_index("ix_run_log_events_channel", "run_log_events", ["channel"], unique=False)
    op.create_index("ix_run_log_events_command", "run_log_events", ["command"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_run_log_events_command", table_name="run_log_events")
    op.drop_index("ix_run_log_events_channel", table_name="run_log_events")
    op.drop_index("ix_run_log_events_invocation_id", table_name="run_log_events")

    with op.batch_alter_table("run_log_events", recreate="auto") as batch_op:
        batch_op.alter_column("run_id", existing_type=sa.String(length=64), nullable=False)
        batch_op.drop_column("working_dir")
        batch_op.drop_column("command")
        batch_op.drop_column("channel")
        batch_op.drop_column("invocation_id")
