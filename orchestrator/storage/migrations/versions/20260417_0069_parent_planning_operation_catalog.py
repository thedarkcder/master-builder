"""add parent planning normalization and planning operations

Revision ID: 20260417_0069
Revises: 20260417_0068
Create Date: 2026-04-17 23:59:00.000000
"""

from __future__ import annotations

from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


revision = "20260417_0069"
down_revision = "20260417_0068"
branch_labels = None
depends_on = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def upgrade() -> None:
    now = _utcnow()
    workflow_type_operations = sa.table(
        "workflow_type_operations",
        sa.column("operation_definition_id", sa.String()),
        sa.column("workflow_type_key", sa.String()),
        sa.column("operation_type", sa.String()),
        sa.column("label", sa.String()),
        sa.column("retry_policy", sa.Text()),
        sa.column("description", sa.Text()),
        sa.column("required", sa.Boolean()),
        sa.column("sort_order", sa.Integer()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    op.bulk_insert(
        workflow_type_operations,
        [
            {
                "operation_definition_id": "legacy-parent-planning:brief_normalization",
                "workflow_type_key": "legacy-parent-planning",
                "operation_type": "brief_normalization",
                "label": "Normalize parent brief",
                "retry_policy": "Re-run when the parent issue changes or when PM clarification answers arrive. Wait for human input when product behavior is still ambiguous.",
                "description": "Normalize the parent issue into the canonical planning brief and decide whether more PM clarification is required.",
                "required": True,
                "sort_order": 5,
                "created_at": now,
                "updated_at": now,
            },
            {
                "operation_definition_id": "legacy-parent-planning:backlog_planning",
                "workflow_type_key": "legacy-parent-planning",
                "operation_type": "backlog_planning",
                "label": "Plan engineering fanout",
                "retry_policy": "Re-run when the normalized brief changes. Wait for human input if the confirmed brief still leaves behavior gaps for child planning.",
                "description": "Build the planning package that determines which engineering child tickets are needed and what behavior they must cover.",
                "required": True,
                "sort_order": 35,
                "created_at": now,
                "updated_at": now,
            },
        ],
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            DELETE FROM workflow_type_operations
            WHERE operation_definition_id IN (
                'legacy-parent-planning:brief_normalization',
                'legacy-parent-planning:backlog_planning'
            )
            """
        )
    )
