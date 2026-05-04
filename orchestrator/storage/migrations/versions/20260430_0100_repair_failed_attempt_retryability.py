"""Repair failed attempt retryability.

Revision ID: 20260430_0100
Revises: 20260430_0099
Create Date: 2026-04-30 15:15:00.000000
"""

from __future__ import annotations

from alembic import op


revision = "20260430_0100"
down_revision = "20260430_0099"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE workflow_operation_attempts
        SET
            retryable = TRUE
        WHERE status IN ('failed', 'retrying')
        """
    )


def downgrade() -> None:
    raise RuntimeError("Failed attempt retryability repair cannot be downgraded")
