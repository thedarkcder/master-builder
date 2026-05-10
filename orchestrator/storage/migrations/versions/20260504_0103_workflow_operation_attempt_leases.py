"""Add workflow operation attempt heartbeat leases.

Revision ID: 20260504_0103
Revises: 20260502_0102
Create Date: 2026-05-04 10:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260504_0103"
down_revision = "20260502_0102"
branch_labels = None
depends_on = None


def _has_column(bind: sa.engine.Connection, table_name: str, column_name: str) -> bool:
    return any(column["name"] == column_name for column in sa.inspect(bind).get_columns(table_name))


def _index_exists(bind: sa.engine.Connection, table_name: str, index_name: str) -> bool:
    return any(index["name"] == index_name for index in sa.inspect(bind).get_indexes(table_name))


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_column(bind, "workflow_operation_attempts", "last_heartbeat_at"):
        op.add_column(
            "workflow_operation_attempts",
            sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        )
    if not _has_column(bind, "workflow_operation_attempts", "lease_expires_at"):
        op.add_column(
            "workflow_operation_attempts",
            sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        )
    if not _has_column(bind, "workflow_operation_attempts", "lease_owner"):
        op.add_column(
            "workflow_operation_attempts",
            sa.Column("lease_owner", sa.String(length=128), nullable=True),
        )
    if not _index_exists(bind, "workflow_operation_attempts", "ix_workflow_operation_attempts_last_heartbeat_at"):
        op.create_index(
            "ix_workflow_operation_attempts_last_heartbeat_at",
            "workflow_operation_attempts",
            ["last_heartbeat_at"],
        )
    if not _index_exists(bind, "workflow_operation_attempts", "ix_workflow_operation_attempts_lease_expires_at"):
        op.create_index(
            "ix_workflow_operation_attempts_lease_expires_at",
            "workflow_operation_attempts",
            ["lease_expires_at"],
        )
    if bind.dialect.name == "postgresql":
        op.execute(
            """
            UPDATE workflow_operation_attempts
            SET
                last_heartbeat_at = COALESCE(last_heartbeat_at, started_at, created_at),
                lease_expires_at = COALESCE(lease_expires_at, started_at, created_at) + INTERVAL '300 seconds',
                lease_owner = COALESCE(lease_owner, 'migration:20260504_0103')
            WHERE status = 'running'
            """
        )
    else:
        op.execute(
            """
            UPDATE workflow_operation_attempts
            SET
                last_heartbeat_at = COALESCE(last_heartbeat_at, started_at, created_at),
                lease_expires_at = datetime(COALESCE(last_heartbeat_at, started_at, created_at), '+300 seconds'),
                lease_owner = COALESCE(lease_owner, 'migration:20260504_0103')
            WHERE status = 'running'
            """
        )


def downgrade() -> None:
    raise RuntimeError("Workflow operation attempt lease migration cannot be downgraded")
