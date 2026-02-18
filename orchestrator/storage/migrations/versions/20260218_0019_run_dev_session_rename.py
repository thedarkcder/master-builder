"""rename codex session id to dev session id

Revision ID: 20260218_0019
Revises: 20260218_0018
Create Date: 2026-02-18 00:00:01.000000
"""

from __future__ import annotations

from alembic import op


revision = "20260218_0019"
down_revision = "20260218_0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_runs_codex_session_id", table_name="runs")
    with op.batch_alter_table("runs", recreate="auto") as batch_op:
        batch_op.alter_column("codex_session_id", new_column_name="dev_session_id")
    op.create_index("ix_runs_dev_session_id", "runs", ["dev_session_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_runs_dev_session_id", table_name="runs")
    with op.batch_alter_table("runs", recreate="auto") as batch_op:
        batch_op.alter_column("dev_session_id", new_column_name="codex_session_id")
    op.create_index("ix_runs_codex_session_id", "runs", ["codex_session_id"], unique=False)
