"""add orchestrated session id to runs

Revision ID: 20260311_0024
Revises: 20260310_0023
Create Date: 2026-03-11 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260311_0024"
down_revision = "20260310_0023"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return any(column.get("name") == column_name for column in inspector.get_columns(table_name))


def _has_index(table_name: str, index_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return any(index.get("name") == index_name for index in inspector.get_indexes(table_name))


def upgrade() -> None:
    if not _has_column("runs", "orchestrated_session_id"):
        with op.batch_alter_table("runs", recreate="auto") as batch_op:
            batch_op.add_column(sa.Column("orchestrated_session_id", sa.String(length=64), nullable=True))

    if not _has_index("runs", "ix_runs_orchestrated_session_id"):
        op.create_index("ix_runs_orchestrated_session_id", "runs", ["orchestrated_session_id"], unique=False)


def downgrade() -> None:
    if _has_index("runs", "ix_runs_orchestrated_session_id"):
        op.drop_index("ix_runs_orchestrated_session_id", table_name="runs")

    if _has_column("runs", "orchestrated_session_id"):
        with op.batch_alter_table("runs", recreate="auto") as batch_op:
            batch_op.drop_column("orchestrated_session_id")
