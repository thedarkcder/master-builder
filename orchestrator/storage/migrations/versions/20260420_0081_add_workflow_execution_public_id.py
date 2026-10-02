"""add public execution ids to workflow executions

Revision ID: 20260420_0081
Revises: 20260420_0080
Create Date: 2026-04-20 23:40:00.000000
"""

from __future__ import annotations

from uuid import uuid4

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision = "20260420_0081"
down_revision = "20260420_0080"
branch_labels = None
depends_on = None


def _workflow_execution_columns(bind: sa.engine.Connection) -> set[str]:
    inspector = inspect(bind)
    return {column["name"] for column in inspector.get_columns("workflow_executions")}


def _workflow_execution_indexes(bind: sa.engine.Connection) -> set[str]:
    inspector = inspect(bind)
    return {index["name"] for index in inspector.get_indexes("workflow_executions")}


def _needs_public_execution_id(
    *, workflow_id: str, execution_id: str, seen: set[str]
) -> bool:
    normalized_workflow_id = str(workflow_id or "").strip()
    normalized_execution_id = str(execution_id or "").strip()
    if not normalized_execution_id:
        return True
    if normalized_execution_id == normalized_workflow_id:
        return True
    if normalized_execution_id in seen:
        return True
    return False


def upgrade() -> None:
    bind = op.get_bind()
    columns = _workflow_execution_columns(bind)

    if "execution_id" not in columns:
        op.add_column(
            "workflow_executions",
            sa.Column("execution_id", sa.String(length=64), nullable=True),
        )

    rows = bind.execute(
        sa.text(
            """
            SELECT workflow_id, execution_id
            FROM workflow_executions
            ORDER BY workflow_id
            """
        )
    ).mappings()

    workflow_executions = sa.table(
        "workflow_executions",
        sa.column("workflow_id", sa.String()),
        sa.column("execution_id", sa.String()),
    )

    seen: set[str] = set()
    for row in rows:
        workflow_id = str(row["workflow_id"] or "").strip()
        raw_execution_id = str(row["execution_id"] or "").strip()
        if not _needs_public_execution_id(
            workflow_id=workflow_id,
            execution_id=raw_execution_id,
            seen=seen,
        ):
            seen.add(raw_execution_id)
            continue
        execution_id = uuid4().hex
        while execution_id in seen:
            execution_id = uuid4().hex
        seen.add(execution_id)
        bind.execute(
            workflow_executions.update()
            .where(workflow_executions.c.workflow_id == workflow_id)
            .values(execution_id=execution_id)
        )

    indexes = _workflow_execution_indexes(bind)
    if "ix_workflow_executions_execution_id" not in indexes:
        op.create_index(
            "ix_workflow_executions_execution_id",
            "workflow_executions",
            ["execution_id"],
            unique=True,
        )

    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("workflow_executions") as batch_op:
            batch_op.alter_column(
                "execution_id", existing_type=sa.String(length=64), nullable=False
            )
    else:
        op.alter_column(
            "workflow_executions",
            "execution_id",
            existing_type=sa.String(length=64),
            nullable=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    columns = _workflow_execution_columns(bind)
    if "execution_id" not in columns:
        return

    indexes = _workflow_execution_indexes(bind)
    if "ix_workflow_executions_execution_id" in indexes:
        op.drop_index(
            "ix_workflow_executions_execution_id", table_name="workflow_executions"
        )

    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("workflow_executions") as batch_op:
            batch_op.drop_column("execution_id")
    else:
        op.drop_column("workflow_executions", "execution_id")
