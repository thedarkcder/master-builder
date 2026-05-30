"""close active operation attempts for terminal workflows

Revision ID: 20260527_0115
Revises: 20260527_0114
Create Date: 2026-05-27 20:35:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260527_0115"
down_revision = "20260527_0114"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    metadata = sa.MetaData()
    workflows = sa.Table("workflow_executions", metadata, autoload_with=bind)
    operations = sa.Table("workflow_operations", metadata, autoload_with=bind)
    attempts = sa.Table("workflow_operation_attempts", metadata, autoload_with=bind)

    rows = bind.execute(
        sa.select(
            attempts.c.attempt_id,
            operations.c.operation_id,
            workflows.c.status.label("workflow_status"),
        )
        .select_from(
            attempts.join(operations, operations.c.operation_id == attempts.c.operation_id).join(
                workflows,
                workflows.c.workflow_id == operations.c.workflow_id,
            )
        )
        .where(
            attempts.c.status == "running",
            workflows.c.status.in_(("completed", "succeeded", "failed", "cancelled")),
        )
    ).mappings()

    for row in rows:
        attempt_id = row["attempt_id"]
        operation_id = row["operation_id"]
        workflow_status = str(row["workflow_status"] or "").strip().lower()
        operation_status = "completed" if workflow_status in {"completed", "succeeded"} else "failed"
        bind.execute(
            attempts.update()
            .where(attempts.c.attempt_id == attempt_id)
            .values(
                status="failed",
                status_detail="Closed active workflow operation attempt because the workflow is already terminal.",
                retryable=False,
                next_retry_at=None,
                lease_expires_at=None,
                finished_at=sa.func.current_timestamp(),
            )
        )
        bind.execute(
            operations.update()
            .where(operations.c.operation_id == operation_id)
            .values(
                status=operation_status,
                finished_at=sa.func.current_timestamp(),
                updated_at=sa.func.current_timestamp(),
            )
        )


def repair_active_terminal_workflow_attempt_rows(
    rows: list[dict[str, object]],
) -> list[dict[str, object]]:
    return [
        {
            **row,
            "attempt_status": "failed",
            "operation_status": (
                "completed"
                if str(row.get("workflow_status") or "").strip().lower() in {"completed", "succeeded"}
                else "failed"
            ),
        }
        for row in rows
    ]


def downgrade() -> None:
    raise RuntimeError("Terminal workflow active operation attempt repair cannot be downgraded")
