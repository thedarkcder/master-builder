"""repair terminal run workflow split brain

Revision ID: 20260527_0116
Revises: 20260527_0115
Create Date: 2026-05-27 21:05:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260527_0116"
down_revision = "20260527_0115"
branch_labels = None
depends_on = None


_FAILED_RUN_STATUSES = ("blocked", "failed", "cancelled")
_SUCCESS_WORKFLOW_STATUSES = ("completed", "succeeded")


def upgrade() -> None:
    bind = op.get_bind()
    metadata = sa.MetaData()
    runs = sa.Table("runs", metadata, autoload_with=bind)
    workflows = sa.Table("workflow_executions", metadata, autoload_with=bind)
    operations = sa.Table("workflow_operations", metadata, autoload_with=bind)
    attempts = sa.Table("workflow_operation_attempts", metadata, autoload_with=bind)

    latest_attempt_by_workflow = (
        sa.select(
            runs.c.workflow_id.label("workflow_id"),
            sa.func.max(runs.c.attempt_number).label("attempt_number"),
        )
        .group_by(runs.c.workflow_id)
        .subquery()
    )
    rows = list(
        bind.execute(
            sa.select(
                runs.c.run_id,
                runs.c.workflow_id,
                runs.c.status.label("run_status"),
                runs.c.last_error,
                runs.c.finished_at,
            )
            .select_from(
                runs.join(
                    latest_attempt_by_workflow,
                    sa.and_(
                        latest_attempt_by_workflow.c.workflow_id == runs.c.workflow_id,
                        latest_attempt_by_workflow.c.attempt_number == runs.c.attempt_number,
                    ),
                ).join(workflows, workflows.c.workflow_id == runs.c.workflow_id)
            )
            .where(
                runs.c.status.in_(_FAILED_RUN_STATUSES),
                workflows.c.status.in_(_SUCCESS_WORKFLOW_STATUSES),
            )
        ).mappings()
    )

    for row in rows:
        message = str(row["last_error"] or "").strip() or (
            f"Run {row['run_id']} finished with terminal status {row['run_status']}."
        )
        bind.execute(
            workflows.update()
            .where(workflows.c.workflow_id == row["workflow_id"])
            .values(
                status="failed",
                last_error=message,
                finished_at=row["finished_at"] or sa.func.current_timestamp(),
                updated_at=sa.func.current_timestamp(),
            )
        )
        operation_rows = list(
            bind.execute(
                sa.select(operations.c.operation_id)
                .where(
                    operations.c.workflow_id == row["workflow_id"],
                    operations.c.run_id == row["run_id"],
                    operations.c.operation_type == "run_attempt_execution",
                    operations.c.status == "completed",
                )
            ).mappings()
        )
        for operation_row in operation_rows:
            bind.execute(
                operations.update()
                .where(operations.c.operation_id == operation_row["operation_id"])
                .values(
                    status="failed",
                    summary=message,
                    finished_at=row["finished_at"] or sa.func.current_timestamp(),
                    updated_at=sa.func.current_timestamp(),
                )
            )
            bind.execute(
                attempts.update()
                .where(
                    attempts.c.operation_id == operation_row["operation_id"],
                    attempts.c.status == "completed",
                )
                .values(
                    status="failed",
                    error_category="run_execution_terminal_status_mismatch",
                    error_message=message,
                    status_detail="Repaired completed workflow operation attempt for failed or blocked run.",
                    retryable=False,
                    next_retry_at=None,
                    lease_expires_at=None,
                    finished_at=row["finished_at"] or sa.func.current_timestamp(),
                )
            )


def repair_terminal_run_workflow_split_brain_rows(
    rows: list[dict[str, object]],
) -> list[dict[str, object]]:
    return [
        {
            **row,
            "workflow_status": "failed",
            "operation_status": "failed",
            "attempt_status": "failed",
        }
        for row in rows
        if str(row.get("run_status") or "").strip().lower() in set(_FAILED_RUN_STATUSES)
        and str(row.get("workflow_status") or "").strip().lower() in set(_SUCCESS_WORKFLOW_STATUSES)
    ]


def downgrade() -> None:
    raise RuntimeError("Terminal run workflow split-brain repair cannot be downgraded")
