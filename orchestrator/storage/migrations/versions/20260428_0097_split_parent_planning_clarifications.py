"""Split parent planning clarification contexts from engineering child contexts.

Revision ID: 20260428_0097
Revises: 20260428_0096
Create Date: 2026-04-28 22:45:00.000000
"""

from __future__ import annotations

from alembic import op


revision = "20260428_0097"
down_revision = "20260428_0096"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(
        """
        UPDATE followup_contexts
        SET
            context_type = 'parent_planning_clarification',
            request_id = 'parent-planning-clarification:' || issue_key,
            metadata_json = (
                metadata_json::jsonb
                || jsonb_build_object(
                    'migrated_from_context_type', 'engineering_clarification',
                    'blocked_operation_type', COALESCE(NULLIF(metadata_json::jsonb->>'blocked_operation_type', ''), 'backlog_planning')
                )
            )::json
        WHERE
            status = 'active'
            AND context_type = 'engineering_clarification'
            AND issue_key IS NOT NULL
            AND metadata_json::jsonb->>'source' = 'workflow_operation_retry'
            AND jsonb_array_length(
                CASE
                    WHEN jsonb_typeof(metadata_json::jsonb->'affected_child_keys') = 'array'
                    THEN metadata_json::jsonb->'affected_child_keys'
                    ELSE '[]'::jsonb
                END
            ) = 0
        """
    )


def downgrade() -> None:
    raise RuntimeError("Parent planning clarification split cannot be downgraded")
