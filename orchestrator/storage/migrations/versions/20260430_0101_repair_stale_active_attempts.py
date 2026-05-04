"""Repair stale active workflow operation attempts.

Revision ID: 20260430_0101
Revises: 20260430_0100
Create Date: 2026-04-30 16:20:00.000000
"""

from __future__ import annotations

from alembic import op


revision = "20260430_0101"
down_revision = "20260430_0100"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        WITH stale_active AS (
            SELECT active_attempt.attempt_id
            FROM workflow_operation_attempts active_attempt
            WHERE active_attempt.status IN ('running', 'waiting_for_input')
              AND EXISTS (
                  SELECT 1
                  FROM workflow_operation_attempts newer_attempt
                  WHERE newer_attempt.operation_id = active_attempt.operation_id
                    AND (
                        newer_attempt.attempt_number > active_attempt.attempt_number
                        OR (
                            newer_attempt.attempt_number = active_attempt.attempt_number
                            AND newer_attempt.created_at > active_attempt.created_at
                        )
                    )
              )
        )
        UPDATE workflow_operation_attempts
        SET
            status = 'superseded',
            status_detail = 'Superseded by a newer persisted attempt during active-attempt invariant repair.',
            retryable = FALSE,
            next_retry_at = NULL,
            finished_at = COALESCE(finished_at, CURRENT_TIMESTAMP)
        WHERE attempt_id IN (SELECT attempt_id FROM stale_active)
        """
    )


def downgrade() -> None:
    raise RuntimeError("Stale active workflow operation attempt repair cannot be downgraded")
