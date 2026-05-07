"""Repair optional Discord projection failures as non-blocking completions.

Revision ID: 20260430_0098
Revises: 20260428_0097
Create Date: 2026-04-30 10:35:00.000000
"""

from __future__ import annotations

from alembic import op


revision = "20260430_0098"
down_revision = "20260428_0097"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE workflow_operation_attempts
        SET
            status = 'completed',
            error_category = NULL,
            error_message = NULL,
            status_detail = NULL,
            retryable = FALSE,
            next_retry_at = NULL,
            finished_at = COALESCE(finished_at, CURRENT_TIMESTAMP)
        WHERE attempt_id IN (
            SELECT attempt.attempt_id
            FROM workflow_operation_attempts attempt
            JOIN workflow_operations operation ON operation.operation_id = attempt.operation_id
            WHERE
                operation.operation_type = 'discord_followup_projection'
                AND attempt.status = 'failed'
                AND attempt.error_message LIKE 'Optional Discord clarification projection did not run for %'
        )
        """
    )
    op.execute(
        """
        UPDATE workflow_operations
        SET
            status = 'completed',
            summary = 'Optional Discord clarification follow-up was not created.',
            finished_at = COALESCE(finished_at, CURRENT_TIMESTAMP),
            updated_at = CURRENT_TIMESTAMP
        WHERE
            operation_type = 'discord_followup_projection'
            AND status = 'failed'
            AND EXISTS (
                SELECT 1
                FROM workflow_operation_attempts latest_attempt
                WHERE
                    latest_attempt.operation_id = workflow_operations.operation_id
                    AND latest_attempt.status = 'completed'
                    AND latest_attempt.attempt_number = (
                        SELECT MAX(candidate.attempt_number)
                        FROM workflow_operation_attempts candidate
                        WHERE candidate.operation_id = workflow_operations.operation_id
                    )
            )
        """
    )


def downgrade() -> None:
    raise RuntimeError("Optional Discord projection status repair cannot be downgraded")
