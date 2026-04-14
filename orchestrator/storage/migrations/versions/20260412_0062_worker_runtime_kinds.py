"""persist registered worker runtime kinds

Revision ID: 20260412_0062
Revises: 20260412_0061
Create Date: 2026-04-12 18:45:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260412_0062"
down_revision = "20260412_0061"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    worker_columns = {column["name"] for column in inspector.get_columns("worker_runtime_states")}
    if "runtime_kinds_json" in worker_columns:
        return

    with op.batch_alter_table("worker_runtime_states") as batch_op:
        batch_op.add_column(
            sa.Column(
                "runtime_kinds_json",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )
    op.execute("UPDATE worker_runtime_states SET runtime_kinds_json = '[]' WHERE runtime_kinds_json IS NULL")
    with op.batch_alter_table("worker_runtime_states") as batch_op:
        batch_op.alter_column("runtime_kinds_json", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("worker_runtime_states") as batch_op:
        batch_op.drop_column("runtime_kinds_json")
