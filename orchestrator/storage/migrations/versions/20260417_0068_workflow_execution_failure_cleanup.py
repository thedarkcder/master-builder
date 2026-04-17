"""remove workflow execution blocked_reason and normalize blocked status

Revision ID: 20260417_0068
Revises: 20260417_0067
Create Date: 2026-04-17 23:45:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260417_0068"
down_revision = "20260417_0067"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return False
    return any(column.get("name") == column_name for column in inspector.get_columns(table_name))


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            UPDATE workflow_executions
            SET status = 'failed',
                finished_at = COALESCE(finished_at, updated_at),
                updated_at = COALESCE(updated_at, created_at)
            WHERE status = 'blocked'
            """
        )
    )
    if _has_column("workflow_executions", "blocked_reason"):
        if bind.dialect.name != "sqlite":
            op.drop_column("workflow_executions", "blocked_reason")
        else:
            with op.batch_alter_table("workflow_executions") as batch_op:
                batch_op.drop_column("blocked_reason")


def downgrade() -> None:
    bind = op.get_bind()
    if not _has_column("workflow_executions", "blocked_reason"):
        if bind.dialect.name != "sqlite":
            op.add_column("workflow_executions", sa.Column("blocked_reason", sa.Text(), nullable=True))
        else:
            with op.batch_alter_table("workflow_executions") as batch_op:
                batch_op.add_column(sa.Column("blocked_reason", sa.Text(), nullable=True))
