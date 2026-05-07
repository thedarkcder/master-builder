"""backfill workflow execution backends from workflow type contracts

Revision ID: 20260420_0078
Revises: 20260420_0077
Create Date: 2026-04-20 16:20:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260420_0078"
down_revision = "20260420_0077"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            UPDATE workflow_executions AS execution
            SET orchestration_backend = workflow_type.orchestration_backend
            FROM workflow_types AS workflow_type
            WHERE workflow_type.workflow_type_key = execution.workflow_type_key
              AND execution.orchestration_backend <> workflow_type.orchestration_backend
            """
        )
    )


def downgrade() -> None:
    # This data repair is not safely reversible.
    return None
