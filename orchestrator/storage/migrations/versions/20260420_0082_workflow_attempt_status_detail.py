"""add workflow attempt status detail

Revision ID: 20260420_0082
Revises: 20260420_0081
Create Date: 2026-04-20 23:55:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision = "20260420_0082"
down_revision = "20260420_0081"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("workflow_operation_attempts")}

    if "status_detail" not in columns:
        if bind.dialect.name == "sqlite":
            with op.batch_alter_table("workflow_operation_attempts") as batch_op:
                batch_op.add_column(sa.Column("status_detail", sa.Text(), nullable=True))
        else:
            op.add_column("workflow_operation_attempts", sa.Column("status_detail", sa.Text(), nullable=True))

    bind.execute(
        sa.text(
            """
            UPDATE workflow_operation_attempts
            SET status_detail = error_message,
                error_message = NULL
            WHERE status = 'waiting_for_input'
              AND error_message IS NOT NULL
              AND (status_detail IS NULL OR status_detail = '')
            """
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("workflow_operation_attempts")}
    if "status_detail" in columns:
        bind.execute(
            sa.text(
                """
                UPDATE workflow_operation_attempts
                SET error_message = status_detail
                WHERE status = 'waiting_for_input'
                  AND status_detail IS NOT NULL
                  AND (error_message IS NULL OR error_message = '')
                """
            )
        )
        if bind.dialect.name == "sqlite":
            with op.batch_alter_table("workflow_operation_attempts") as batch_op:
                batch_op.drop_column("status_detail")
        else:
            op.drop_column("workflow_operation_attempts", "status_detail")
