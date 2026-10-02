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

SOURCE_EXTERNAL_ID_INDEX = "ix_workflow_executions_source_external_id"
SOURCE_EXTERNAL_ID_INDEX_COLUMNS = [
    "tenant_id",
    "source_system",
    "source_external_id",
    "dedupe_scope",
]


def _table_exists(bind: sa.engine.Connection, table_name: str) -> bool:
    return sa.inspect(bind).has_table(table_name)


def _column_exists(
    bind: sa.engine.Connection, table_name: str, column_name: str
) -> bool:
    return any(
        column["name"] == column_name
        for column in sa.inspect(bind).get_columns(table_name)
    )


def _index_columns(
    bind: sa.engine.Connection, table_name: str, index_name: str
) -> list[str] | None:
    for index in sa.inspect(bind).get_indexes(table_name):
        if index["name"] == index_name:
            return list(index.get("column_names") or [])
    return None


def upgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind, "workflow_executions"):
        return
    if not _column_exists(bind, "workflow_executions", "source_external_id"):
        op.add_column(
            "workflow_executions",
            sa.Column("source_external_id", sa.String(length=255), nullable=True),
        )
    existing_index_columns = _index_columns(
        bind, "workflow_executions", SOURCE_EXTERNAL_ID_INDEX
    )
    if existing_index_columns != SOURCE_EXTERNAL_ID_INDEX_COLUMNS:
        if existing_index_columns is not None:
            op.drop_index(SOURCE_EXTERNAL_ID_INDEX, table_name="workflow_executions")
        op.create_index(
            SOURCE_EXTERNAL_ID_INDEX,
            "workflow_executions",
            SOURCE_EXTERNAL_ID_INDEX_COLUMNS,
        )


def downgrade() -> None:
    raise RuntimeError(
        "Workflow execution source external id migration cannot be downgraded"
    )
