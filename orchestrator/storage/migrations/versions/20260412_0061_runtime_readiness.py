"""persist runtime requirements for runs and runtime readiness for workers

Revision ID: 20260412_0061
Revises: 20260412_0060
Create Date: 2026-04-12 19:20:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260412_0061"
down_revision = "20260412_0060"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("runs") as batch_op:
        batch_op.add_column(
            sa.Column(
                "required_runtime_kinds_json",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )
    with op.batch_alter_table("worker_runtime_states") as batch_op:
        batch_op.add_column(
            sa.Column(
                "runtime_dependencies_json",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            )
        )
    op.execute("UPDATE runs SET required_runtime_kinds_json = '[]' WHERE required_runtime_kinds_json IS NULL")
    op.execute(
        "UPDATE worker_runtime_states SET runtime_dependencies_json = '{}' WHERE runtime_dependencies_json IS NULL"
    )
    with op.batch_alter_table("runs") as batch_op:
        batch_op.alter_column("required_runtime_kinds_json", server_default=None)
    with op.batch_alter_table("worker_runtime_states") as batch_op:
        batch_op.alter_column("runtime_dependencies_json", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("worker_runtime_states") as batch_op:
        batch_op.drop_column("runtime_dependencies_json")
    with op.batch_alter_table("runs") as batch_op:
        batch_op.drop_column("required_runtime_kinds_json")
