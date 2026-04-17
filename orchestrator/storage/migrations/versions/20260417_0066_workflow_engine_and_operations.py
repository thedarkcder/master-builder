"""add workflow engine backend and workflow operations

Revision ID: 20260417_0066
Revises: 20260417_0065
Create Date: 2026-04-17 19:15:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260417_0066"
down_revision = "20260417_0065"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workflow_executions",
        sa.Column("orchestration_backend", sa.String(length=32), nullable=False, server_default="legacy"),
    )
    op.execute("UPDATE workflow_executions SET orchestration_backend = 'legacy' WHERE orchestration_backend IS NULL")
    if op.get_bind().dialect.name != "sqlite":
        op.alter_column("workflow_executions", "orchestration_backend", server_default=None)

    op.create_table(
        "workflow_blockers",
        sa.Column("blocker_id", sa.String(length=64), nullable=False),
        sa.Column("workflow_id", sa.String(length=64), nullable=False),
        sa.Column("operation_id", sa.String(length=64), nullable=True),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workflow_id"], ["workflow_executions.workflow_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("blocker_id"),
    )
    op.create_index("ix_workflow_blockers_workflow_id", "workflow_blockers", ["workflow_id"])
    op.create_index("ix_workflow_blockers_status", "workflow_blockers", ["status"])

    op.create_table(
        "workflow_operations",
        sa.Column("operation_id", sa.String(length=64), nullable=False),
        sa.Column("workflow_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=True),
        sa.Column("operation_type", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("target_system", sa.String(length=64), nullable=True),
        sa.Column("target_ref", sa.String(length=255), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("blocker_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["blocker_id"], ["workflow_blockers.blocker_id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.run_id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workflow_id"], ["workflow_executions.workflow_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("operation_id"),
        sa.UniqueConstraint("workflow_id", "idempotency_key", name="uq_workflow_operations_idempotency"),
    )
    op.create_index("ix_workflow_operations_workflow_id", "workflow_operations", ["workflow_id"])
    op.create_index("ix_workflow_operations_run_id", "workflow_operations", ["run_id"])
    op.create_index("ix_workflow_operations_status", "workflow_operations", ["status"])

    op.create_table(
        "workflow_operation_attempts",
        sa.Column("attempt_id", sa.String(length=64), nullable=False),
        sa.Column("operation_id", sa.String(length=64), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error_category", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("retryable", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["operation_id"], ["workflow_operations.operation_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("attempt_id"),
        sa.UniqueConstraint("operation_id", "attempt_number", name="uq_workflow_operation_attempt_number"),
    )
    op.create_index("ix_workflow_operation_attempts_operation_id", "workflow_operation_attempts", ["operation_id"])
    op.create_index("ix_workflow_operation_attempts_status", "workflow_operation_attempts", ["status"])

    if op.get_bind().dialect.name != "sqlite":
        op.create_foreign_key(
            "fk_workflow_blockers_operation_id",
            "workflow_blockers",
            "workflow_operations",
            ["operation_id"],
            ["operation_id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    if op.get_bind().dialect.name != "sqlite":
        op.drop_constraint("fk_workflow_blockers_operation_id", "workflow_blockers", type_="foreignkey")
    op.drop_index("ix_workflow_operation_attempts_status", table_name="workflow_operation_attempts")
    op.drop_index("ix_workflow_operation_attempts_operation_id", table_name="workflow_operation_attempts")
    op.drop_table("workflow_operation_attempts")
    op.drop_index("ix_workflow_operations_status", table_name="workflow_operations")
    op.drop_index("ix_workflow_operations_run_id", table_name="workflow_operations")
    op.drop_index("ix_workflow_operations_workflow_id", table_name="workflow_operations")
    op.drop_table("workflow_operations")
    op.drop_index("ix_workflow_blockers_status", table_name="workflow_blockers")
    op.drop_index("ix_workflow_blockers_workflow_id", table_name="workflow_blockers")
    op.drop_table("workflow_blockers")
    op.drop_column("workflow_executions", "orchestration_backend")
