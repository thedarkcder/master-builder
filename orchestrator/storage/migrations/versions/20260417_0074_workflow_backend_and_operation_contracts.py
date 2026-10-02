"""backfill workflow backend and enforce workflow contracts

Revision ID: 20260417_0074
Revises: 20260417_0073
Create Date: 2026-04-17 16:10:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260417_0074"
down_revision = "20260417_0073"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    workflow_rows = (
        bind.execute(
            sa.text(
                """
            SELECT workflow_id, workflow_type_key
            FROM workflow_executions
            WHERE orchestration_backend IS NULL OR TRIM(orchestration_backend) = ''
            """
            )
        )
        .mappings()
        .all()
    )
    for row in workflow_rows:
        backend = bind.execute(
            sa.text(
                """
                SELECT orchestration_backend
                FROM workflow_types
                WHERE workflow_type_key = :workflow_type_key
                """
            ),
            {"workflow_type_key": row["workflow_type_key"]},
        ).scalar_one_or_none()
        normalized_backend = str(backend or "").strip().lower()
        if not normalized_backend:
            raise RuntimeError(
                f"workflow_type {row['workflow_type_key']} is missing orchestration_backend for workflow {row['workflow_id']}"
            )
        bind.execute(
            sa.text(
                """
                UPDATE workflow_executions
                SET orchestration_backend = :orchestration_backend
                WHERE workflow_id = :workflow_id
                """
            ),
            {
                "workflow_id": row["workflow_id"],
                "orchestration_backend": normalized_backend,
            },
        )

    invalid_backends = bind.execute(
        sa.text(
            """
            SELECT COUNT(*)
            FROM workflow_types
            WHERE orchestration_backend NOT IN ('legacy', 'temporal', 'database')
            """
        )
    ).scalar_one()
    if invalid_backends:
        raise RuntimeError(
            "workflow_types contains unsupported orchestration_backend values"
        )

    invalid_execution_backends = bind.execute(
        sa.text(
            """
            SELECT COUNT(*)
            FROM workflow_executions
            WHERE orchestration_backend NOT IN ('legacy', 'temporal', 'database')
            """
        )
    ).scalar_one()
    if invalid_execution_backends:
        raise RuntimeError(
            "workflow_executions contains unsupported orchestration_backend values"
        )

    undefined_operations = (
        bind.execute(
            sa.text(
                """
            SELECT we.workflow_id, wo.operation_type, we.workflow_type_key
            FROM workflow_operations wo
            JOIN workflow_executions we ON we.workflow_id = wo.workflow_id
            LEFT JOIN workflow_type_operations wto
              ON wto.workflow_type_key = we.workflow_type_key
             AND wto.operation_type = wo.operation_type
            WHERE wto.operation_definition_id IS NULL
            ORDER BY we.workflow_id, wo.operation_type
            """
            )
        )
        .mappings()
        .all()
    )
    if undefined_operations:
        formatted = ", ".join(
            f"{row['workflow_id']}:{row['operation_type']}"
            for row in undefined_operations[:10]
        )
        raise RuntimeError(
            f"workflow_operations contains undefined operation types: {formatted}"
        )

    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("workflow_types") as batch_op:
            batch_op.create_check_constraint(
                "ck_workflow_types_orchestration_backend",
                "orchestration_backend IN ('legacy', 'temporal', 'database')",
            )
        with op.batch_alter_table("workflow_executions") as batch_op:
            batch_op.create_check_constraint(
                "ck_workflow_executions_orchestration_backend",
                "orchestration_backend IN ('legacy', 'temporal', 'database')",
            )
    else:
        op.create_check_constraint(
            "ck_workflow_types_orchestration_backend",
            "workflow_types",
            "orchestration_backend IN ('legacy', 'temporal', 'database')",
        )
        op.create_check_constraint(
            "ck_workflow_executions_orchestration_backend",
            "workflow_executions",
            "orchestration_backend IN ('legacy', 'temporal', 'database')",
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("workflow_executions") as batch_op:
            batch_op.drop_constraint(
                "ck_workflow_executions_orchestration_backend", type_="check"
            )
        with op.batch_alter_table("workflow_types") as batch_op:
            batch_op.drop_constraint(
                "ck_workflow_types_orchestration_backend", type_="check"
            )
    else:
        op.drop_constraint(
            "ck_workflow_executions_orchestration_backend",
            "workflow_executions",
            type_="check",
        )
        op.drop_constraint(
            "ck_workflow_types_orchestration_backend", "workflow_types", type_="check"
        )
