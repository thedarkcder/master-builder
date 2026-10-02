"""add workflow type system keys

Revision ID: 20260417_0072
Revises: 20260417_0071
Create Date: 2026-04-17 15:40:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260417_0072"
down_revision = "20260417_0071"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return False
    return any(
        column.get("name") == column_name
        for column in inspector.get_columns(table_name)
    )


def _has_index(table_name: str, index_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return False
    return any(
        index.get("name") == index_name for index in inspector.get_indexes(table_name)
    )


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_column("workflow_types", "system_key"):
        if bind.dialect.name != "sqlite":
            op.add_column(
                "workflow_types",
                sa.Column("system_key", sa.String(length=64), nullable=True),
            )
        else:
            with op.batch_alter_table("workflow_types") as batch_op:
                batch_op.add_column(
                    sa.Column("system_key", sa.String(length=64), nullable=True)
                )

    bind.execute(
        sa.text(
            """
            UPDATE workflow_types
            SET system_key = CASE workflow_type_key
                WHEN 'issue_execution' THEN 'issue_execution'
                WHEN 'parent_planning' THEN 'parent_planning'
                WHEN 'pr_remediation' THEN 'pr_remediation'
                ELSE workflow_type_key
            END
            """
        )
    )

    missing = bind.execute(
        sa.text("SELECT COUNT(*) FROM workflow_types WHERE system_key IS NULL")
    ).scalar_one()
    if missing:
        raise RuntimeError(
            "workflow_types contains rows without a backfilled system_key"
        )

    if bind.dialect.name != "sqlite":
        op.alter_column("workflow_types", "system_key", nullable=False)
        if not _has_index("workflow_types", "ix_workflow_types_system_key"):
            op.create_index(
                "ix_workflow_types_system_key",
                "workflow_types",
                ["system_key"],
                unique=True,
            )
    else:
        with op.batch_alter_table("workflow_types") as batch_op:
            batch_op.alter_column("system_key", nullable=False)
            if not _has_index("workflow_types", "ix_workflow_types_system_key"):
                batch_op.create_index(
                    "ix_workflow_types_system_key", ["system_key"], unique=True
                )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        op.drop_index("ix_workflow_types_system_key", table_name="workflow_types")
        op.drop_column("workflow_types", "system_key")
    else:
        with op.batch_alter_table("workflow_types") as batch_op:
            batch_op.drop_index("ix_workflow_types_system_key")
            batch_op.drop_column("system_key")
