"""Enforce one active workflow operation attempt.

Revision ID: 20260430_0099
Revises: 20260430_0098
Create Date: 2026-04-30 14:30:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260430_0099"
down_revision = "20260430_0098"
branch_labels = None
depends_on = None

ACTIVE_ATTEMPT_STATUSES = "'running', 'waiting_for_input'"


def _index_exists(index_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return any(
        index["name"] == index_name
        for index in inspector.get_indexes("workflow_operation_attempts")
    )


def upgrade() -> None:
    op.execute(
        f"""
        WITH ranked AS (
            SELECT
                attempt_id,
                ROW_NUMBER() OVER (
                    PARTITION BY operation_id
                    ORDER BY attempt_number DESC, created_at DESC, attempt_id DESC
                ) AS active_rank
            FROM workflow_operation_attempts
            WHERE status IN ({ACTIVE_ATTEMPT_STATUSES})
        )
        UPDATE workflow_operation_attempts
        SET
            status = 'superseded',
            status_detail = 'Superseded by a newer active attempt during active-attempt invariant repair.',
            retryable = FALSE,
            next_retry_at = NULL,
            finished_at = COALESCE(finished_at, CURRENT_TIMESTAMP)
        WHERE attempt_id IN (
            SELECT attempt_id
            FROM ranked
            WHERE active_rank > 1
        )
        """
    )
    if not _index_exists("uq_workflow_operation_active_attempt"):
        op.create_index(
            "uq_workflow_operation_active_attempt",
            "workflow_operation_attempts",
            ["operation_id"],
            unique=True,
            postgresql_where=sa.text("status IN ('running', 'waiting_for_input')"),
            sqlite_where=sa.text("status IN ('running', 'waiting_for_input')"),
        )


def downgrade() -> None:
    raise RuntimeError(
        "Single active workflow operation attempt enforcement cannot be downgraded"
    )
