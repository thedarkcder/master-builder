"""backfill failed workflow attempts as retryable

Revision ID: 20260420_0083
Revises: 20260420_0082
Create Date: 2026-04-20 23:59:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260420_0083"
down_revision = "20260420_0082"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            UPDATE workflow_operation_attempts
            SET retryable = TRUE
            WHERE status IN ('failed', 'retrying')
            """
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            UPDATE workflow_operation_attempts
            SET retryable = FALSE
            WHERE status IN ('failed', 'retrying')
            """
        )
    )
