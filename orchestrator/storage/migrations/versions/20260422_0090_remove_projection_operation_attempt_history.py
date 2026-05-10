"""Remove attempt history for projection-only workflow operations.

Revision ID: 20260422_0090
Revises: 20260422_0089
Create Date: 2026-04-22 17:30:00.000000
"""

from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision = "20260422_0090"
down_revision = "20260422_0089"
branch_labels = None
depends_on = None


_PROJECTION_OPERATION_TYPES = (
    "brief_normalization",
    "jira_parent_update",
    "jira_comment_projection",
    "discord_followup_projection",
)


def _projection_operation_filter() -> str:
    values = ", ".join(f"'{item}'" for item in _PROJECTION_OPERATION_TYPES)
    return f"SELECT operation_id FROM workflow_operations WHERE operation_type IN ({values})"


def upgrade() -> None:
    operation_filter = _projection_operation_filter()
    op.execute(
        f"""
        DELETE FROM observability_stream_events
        WHERE operation_id IN ({operation_filter})
        """
    )
    op.execute(
        f"""
        DELETE FROM audit_events
        WHERE operation_id IN ({operation_filter})
        """
    )
    op.execute(
        f"""
        DELETE FROM workflow_operation_attempts
        WHERE operation_id IN ({operation_filter})
        """
    )


def downgrade() -> None:
    # Destructive cleanup; historical projection attempts are intentionally not restored.
    return None
