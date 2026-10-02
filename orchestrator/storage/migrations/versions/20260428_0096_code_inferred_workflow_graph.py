"""Move workflow definitions from database catalog to code.

Revision ID: 20260428_0096
Revises: 20260424_0095
Create Date: 2026-04-28 17:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260428_0096"
down_revision = "20260424_0095"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table_name in inspector.get_table_names()


def _fk_names(table_name: str) -> list[str]:
    inspector = sa.inspect(op.get_bind())
    return [
        fk["name"] for fk in inspector.get_foreign_keys(table_name) if fk.get("name")
    ]


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if _table_exists("workflow_executions"):
        workflow_execution_fk_names = [
            fk_name
            for fk_name in _fk_names("workflow_executions")
            if "workflow_type" in fk_name
        ]
        if dialect == "sqlite":
            if workflow_execution_fk_names:
                with op.batch_alter_table("workflow_executions") as batch_op:
                    for fk_name in workflow_execution_fk_names:
                        batch_op.drop_constraint(fk_name, type_="foreignkey")
        else:
            for fk_name in workflow_execution_fk_names:
                op.drop_constraint(fk_name, "workflow_executions", type_="foreignkey")

    if _table_exists("workflow_type_operations"):
        op.drop_table("workflow_type_operations")

    if _table_exists("workflow_types"):
        op.drop_table("workflow_types")


def downgrade() -> None:
    raise RuntimeError("Code-inferred workflow graph migration cannot be downgraded")
