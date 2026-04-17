"""mark parent planning clarification projection operations as optional

Revision ID: 20260417_0070
Revises: 20260417_0069
Create Date: 2026-04-17 15:05:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260417_0070"
down_revision = "20260417_0069"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    table = sa.table(
        "workflow_type_operations",
        sa.column("workflow_type_key", sa.String()),
        sa.column("operation_type", sa.String()),
        sa.column("required", sa.Boolean()),
    )
    bind.execute(
        table.update()
        .where(
            sa.and_(
                table.c.workflow_type_key == "legacy-parent-planning",
                table.c.operation_type == "jira_comment_projection",
            )
        )
        .values(required=False)
    )


def downgrade() -> None:
    bind = op.get_bind()
    table = sa.table(
        "workflow_type_operations",
        sa.column("workflow_type_key", sa.String()),
        sa.column("operation_type", sa.String()),
        sa.column("required", sa.Boolean()),
    )
    bind.execute(
        table.update()
        .where(
            sa.and_(
                table.c.workflow_type_key == "legacy-parent-planning",
                table.c.operation_type == "jira_comment_projection",
            )
        )
        .values(required=True)
    )
