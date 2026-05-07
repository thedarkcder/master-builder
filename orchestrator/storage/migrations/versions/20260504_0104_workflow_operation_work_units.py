"""Add durable workflow operation work units.

Revision ID: 20260504_0104
Revises: 20260504_0103
Create Date: 2026-05-04 16:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260504_0104"
down_revision = "20260504_0103"
branch_labels = None
depends_on = None


def _table_exists(bind: sa.engine.Connection, table_name: str) -> bool:
    return sa.inspect(bind).has_table(table_name)


def upgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind, "workflow_operation_work_units"):
        op.create_table(
            "workflow_operation_work_units",
            sa.Column("work_unit_id", sa.String(length=64), primary_key=True),
            sa.Column(
                "operation_id",
                sa.String(length=64),
                sa.ForeignKey("workflow_operations.operation_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "parent_attempt_id",
                sa.String(length=64),
                sa.ForeignKey("workflow_operation_attempts.attempt_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("unit_key", sa.String(length=128), nullable=False),
            sa.Column("unit_kind", sa.String(length=32), nullable=False),
            sa.Column("idempotency_key", sa.String(length=255), nullable=False),
            sa.Column("input_fingerprint", sa.String(length=64), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("output_json", sa.JSON(), nullable=True),
            sa.Column("error_category", sa.String(length=64), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint(
                "operation_id",
                "unit_key",
                "idempotency_key",
                "input_fingerprint",
                name="uq_workflow_operation_work_unit_identity",
            ),
        )
        op.create_index("ix_workflow_operation_work_units_operation_id", "workflow_operation_work_units", ["operation_id"])
        op.create_index(
            "ix_workflow_operation_work_units_parent_attempt_id",
            "workflow_operation_work_units",
            ["parent_attempt_id"],
        )
        op.create_index("ix_workflow_operation_work_units_unit_key", "workflow_operation_work_units", ["unit_key"])
        op.create_index("ix_workflow_operation_work_units_status", "workflow_operation_work_units", ["status"])
    if not _table_exists(bind, "workflow_operation_work_unit_attempts"):
        op.create_table(
            "workflow_operation_work_unit_attempts",
            sa.Column("work_unit_attempt_id", sa.String(length=64), primary_key=True),
            sa.Column(
                "work_unit_id",
                sa.String(length=64),
                sa.ForeignKey("workflow_operation_work_units.work_unit_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "operation_attempt_id",
                sa.String(length=64),
                sa.ForeignKey("workflow_operation_attempts.attempt_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("attempt_number", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("error_category", sa.String(length=64), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("work_unit_id", "attempt_number", name="uq_workflow_operation_work_unit_attempt_number"),
        )
        op.create_index(
            "ix_workflow_operation_work_unit_attempts_work_unit_id",
            "workflow_operation_work_unit_attempts",
            ["work_unit_id"],
        )
        op.create_index(
            "ix_workflow_operation_work_unit_attempts_operation_attempt_id",
            "workflow_operation_work_unit_attempts",
            ["operation_attempt_id"],
        )
        op.create_index(
            "ix_workflow_operation_work_unit_attempts_status",
            "workflow_operation_work_unit_attempts",
            ["status"],
        )


def downgrade() -> None:
    raise RuntimeError("Workflow operation work unit migration cannot be downgraded")
