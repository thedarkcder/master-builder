"""persist runtime requirements for runs and runtime readiness for workers

Revision ID: 20260412_0061
Revises: 20260412_0060
Create Date: 2026-04-12 19:20:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision = "20260412_0061"
down_revision = "20260412_0060"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    run_columns = {column.get("name") for column in inspector.get_columns("runs")}
    worker_columns = {column.get("name") for column in inspector.get_columns("worker_runtime_states")}
    added_required_runtime_kinds = False
    added_runtime_dependencies = False
    with op.batch_alter_table("runs") as batch_op:
        if "required_runtime_kinds_json" not in run_columns:
            batch_op.add_column(
                sa.Column(
                    "required_runtime_kinds_json",
                    sa.JSON(),
                    nullable=False,
                    server_default=sa.text("'[]'"),
                )
            )
            added_required_runtime_kinds = True
    with op.batch_alter_table("worker_runtime_states") as batch_op:
        if "runtime_dependencies_json" not in worker_columns:
            batch_op.add_column(
                sa.Column(
                    "runtime_dependencies_json",
                    sa.JSON(),
                    nullable=False,
                    server_default=sa.text("'{}'"),
                )
            )
            added_runtime_dependencies = True
    if "required_runtime_kinds_json" in run_columns or added_required_runtime_kinds:
        op.execute("UPDATE runs SET required_runtime_kinds_json = '[]' WHERE required_runtime_kinds_json IS NULL")
    if "runtime_dependencies_json" in worker_columns or added_runtime_dependencies:
        op.execute(
            "UPDATE worker_runtime_states SET runtime_dependencies_json = '{}' WHERE runtime_dependencies_json IS NULL"
        )
    if added_required_runtime_kinds:
        with op.batch_alter_table("runs") as batch_op:
            batch_op.alter_column("required_runtime_kinds_json", server_default=None)
    if added_runtime_dependencies:
        with op.batch_alter_table("worker_runtime_states") as batch_op:
            batch_op.alter_column("runtime_dependencies_json", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("worker_runtime_states") as batch_op:
        batch_op.drop_column("runtime_dependencies_json")
    with op.batch_alter_table("runs") as batch_op:
        batch_op.drop_column("required_runtime_kinds_json")
