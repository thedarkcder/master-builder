"""Add stable external source identity to workflow executions.

Revision ID: 20260505_0106
Revises: 20260504_0105
Create Date: 2026-05-05 12:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260505_0106"
down_revision = "20260504_0105"
branch_labels = None
depends_on = None


def _table_exists(bind: sa.engine.Connection, table_name: str) -> bool:
    return sa.inspect(bind).has_table(table_name)


def _column_exists(bind: sa.engine.Connection, table_name: str, column_name: str) -> bool:
    return any(column["name"] == column_name for column in sa.inspect(bind).get_columns(table_name))


def _index_exists(bind: sa.engine.Connection, table_name: str, index_name: str) -> bool:
    return any(index["name"] == index_name for index in sa.inspect(bind).get_indexes(table_name))


def upgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind, "workflow_executions"):
        return
    if not _column_exists(bind, "workflow_executions", "source_external_id"):
        op.add_column("workflow_executions", sa.Column("source_external_id", sa.String(length=255), nullable=True))
    if not _index_exists(bind, "workflow_executions", "ix_workflow_executions_source_external_id"):
        op.create_index(
            "ix_workflow_executions_source_external_id",
            "workflow_executions",
            ["tenant_id", "source_system", "source_external_id", "dedupe_scope"],
        )


def downgrade() -> None:
    raise RuntimeError("Workflow execution source external id migration cannot be downgraded")

