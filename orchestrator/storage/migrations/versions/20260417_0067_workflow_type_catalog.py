"""add workflow type catalog

Revision ID: 20260417_0067
Revises: 20260417_0066
Create Date: 2026-04-17 22:10:00.000000
"""

from __future__ import annotations

from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


revision = "20260417_0067"
down_revision = "20260417_0066"
branch_labels = None
depends_on = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def upgrade() -> None:
    now = _utcnow()
    op.create_table(
        "workflow_types",
        sa.Column("workflow_type_key", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("workflow_type_key"),
    )
    op.create_table(
        "workflow_type_operations",
        sa.Column("operation_definition_id", sa.String(length=64), nullable=False),
        sa.Column("workflow_type_key", sa.String(length=64), nullable=False),
        sa.Column("operation_type", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("retry_policy", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("required", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workflow_type_key"], ["workflow_types.workflow_type_key"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("operation_definition_id"),
        sa.UniqueConstraint("workflow_type_key", "operation_type", name="uq_workflow_type_operations_key_type"),
        sa.UniqueConstraint("workflow_type_key", "sort_order", name="uq_workflow_type_operations_key_order"),
    )
    op.create_index("ix_workflow_type_operations_workflow_type_key", "workflow_type_operations", ["workflow_type_key"])

    workflow_types = sa.table(
        "workflow_types",
        sa.column("workflow_type_key", sa.String()),
        sa.column("label", sa.String()),
        sa.column("description", sa.Text()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
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
        workflow_types,
        [
            {
                "workflow_type_key": "issue_execution",
                "label": "Issue Execution",
                "description": "Central development-team execution workflow that progresses PM, Dev, Test, and Review with durable human-input handling.",
                "created_at": now,
                "updated_at": now,
            },
            {
                "workflow_type_key": "legacy-parent-planning",
                "label": "Parent Planning",
                "description": "Parent feature planning workflow that confirms the brief, collects clarification, and fans out engineering child tickets.",
                "created_at": now,
                "updated_at": now,
            },
        ],
    )
    op.bulk_insert(
        workflow_type_operations,
        [
            {
                "operation_definition_id": "issue_execution:run_attempt_execution",
                "workflow_type_key": "issue_execution",
                "operation_type": "run_attempt_execution",
                "label": "Execute run attempt",
                "retry_policy": "Retry transient worker or dispatch failures. Block if the run cannot be resumed automatically.",
                "description": "Dispatch the current run attempt through the central execution engine.",
                "required": True,
                "sort_order": 10,
                "created_at": now,
                "updated_at": now,
            },
            {
                "operation_definition_id": "issue_execution:human_input_resume",
                "workflow_type_key": "issue_execution",
                "operation_type": "human_input_resume",
                "label": "Resume from human input",
                "retry_policy": "Do not auto-retry invalid or stale human-input resumes. Retry only on transient infrastructure failures.",
                "description": "Consume a pending human answer and continue the execution workflow.",
                "required": False,
                "sort_order": 20,
                "created_at": now,
                "updated_at": now,
            },
            {
                "operation_definition_id": "issue_execution:jira_comment_projection",
                "workflow_type_key": "issue_execution",
                "operation_type": "jira_comment_projection",
                "label": "Project Jira updates",
                "retry_policy": "Retry transient Jira failures. Block on permanent Jira validation errors.",
                "description": "Project execution status or clarification updates back to Jira.",
                "required": False,
                "sort_order": 30,
                "created_at": now,
                "updated_at": now,
            },
            {
                "operation_definition_id": "issue_execution:discord_followup_projection",
                "workflow_type_key": "issue_execution",
                "operation_type": "discord_followup_projection",
                "label": "Project Discord updates",
                "retry_policy": "Retry transient Discord failures. Block if the configured follow-up target is invalid.",
                "description": "Project execution follow-up messages into Discord threads or channels.",
                "required": False,
                "sort_order": 40,
                "created_at": now,
                "updated_at": now,
            },
            {
                "operation_definition_id": "issue_execution:notification_emit",
                "workflow_type_key": "issue_execution",
                "operation_type": "notification_emit",
                "label": "Emit workflow notifications",
                "retry_policy": "Retry transient notification delivery failures. Keep notifications idempotent by unresolved state fingerprint.",
                "description": "Open or resolve notifications tied to execution blockers or required intervention.",
                "required": False,
                "sort_order": 50,
                "created_at": now,
                "updated_at": now,
            },
            {
                "operation_definition_id": "legacy-parent-planning:jira_parent_update",
                "workflow_type_key": "legacy-parent-planning",
                "operation_type": "jira_parent_update",
                "label": "Sync parent issue",
                "retry_policy": "Retry transient Jira failures. Block on content or contract errors.",
                "description": "Write the current parent-issue brief and sync state back to Jira.",
                "required": True,
                "sort_order": 10,
                "created_at": now,
                "updated_at": now,
            },
            {
                "operation_definition_id": "legacy-parent-planning:jira_comment_projection",
                "workflow_type_key": "legacy-parent-planning",
                "operation_type": "jira_comment_projection",
                "label": "Project clarification comments",
                "retry_policy": "Retry transient Jira failures. Keep one outward write per unresolved clarification state.",
                "description": "Post Jira clarification comments and system follow-ups.",
                "required": True,
                "sort_order": 20,
                "created_at": now,
                "updated_at": now,
            },
            {
                "operation_definition_id": "legacy-parent-planning:discord_followup_projection",
                "workflow_type_key": "legacy-parent-planning",
                "operation_type": "discord_followup_projection",
                "label": "Project Discord follow-up",
                "retry_policy": "Retry transient Discord failures. Block if the configured follow-up target is invalid.",
                "description": "Project PM clarification and follow-up state into Discord when configured.",
                "required": False,
                "sort_order": 30,
                "created_at": now,
                "updated_at": now,
            },
            {
                "operation_definition_id": "legacy-parent-planning:jira_child_fanout",
                "workflow_type_key": "legacy-parent-planning",
                "operation_type": "jira_child_fanout",
                "label": "Fan out engineering child tickets",
                "retry_policy": "Retry transient Jira failures. Block on permanent Jira validation errors such as content limits.",
                "description": "Create or refresh the engineering child tickets implied by the confirmed parent brief.",
                "required": True,
                "sort_order": 40,
                "created_at": now,
                "updated_at": now,
            },
            {
                "operation_definition_id": "legacy-parent-planning:notification_emit",
                "workflow_type_key": "legacy-parent-planning",
                "operation_type": "notification_emit",
                "label": "Emit workflow notifications",
                "retry_policy": "Retry transient notification delivery failures. Keep notifications idempotent by unresolved state fingerprint.",
                "description": "Open or resolve user/admin notifications tied to workflow blockers or remediation.",
                "required": False,
                "sort_order": 50,
                "created_at": now,
                "updated_at": now,
            },
        ],
    )

    op.add_column("workflow_executions", sa.Column("workflow_type_key", sa.String(length=64), nullable=True))
    op.create_index("ix_workflow_executions_workflow_type_key", "workflow_executions", ["workflow_type_key"])

    op.execute(
        """
        UPDATE workflow_executions
        SET workflow_type_key = CASE
            WHEN workflow_id LIKE 'legacy-parent-planning:%' THEN 'legacy-parent-planning'
            WHEN dedupe_scope = 'issue_execution' THEN 'issue_execution'
            WHEN dedupe_scope = 'legacy-parent-planning' THEN 'legacy-parent-planning'
            WHEN dedupe_scope = 'parent_planning' THEN 'legacy-parent-planning'
            ELSE workflow_type_key
        END
        """
    )

    bind = op.get_bind()
    missing = bind.execute(sa.text("SELECT COUNT(*) FROM workflow_executions WHERE workflow_type_key IS NULL")).scalar_one()
    if int(missing or 0) != 0:
        raise RuntimeError("workflow_executions contains rows without a backfilled workflow_type_key")

    if bind.dialect.name != "sqlite":
        op.alter_column("workflow_executions", "workflow_type_key", nullable=False)
    else:
        with op.batch_alter_table("workflow_executions") as batch_op:
            batch_op.alter_column("workflow_type_key", nullable=False)
    if bind.dialect.name != "sqlite":
        op.create_foreign_key(
            "fk_workflow_executions_workflow_type_key",
            "workflow_executions",
            "workflow_types",
            ["workflow_type_key"],
            ["workflow_type_key"],
            ondelete="RESTRICT",
        )
    else:
        with op.batch_alter_table("workflow_executions") as batch_op:
            batch_op.create_foreign_key(
                "fk_workflow_executions_workflow_type_key",
                "workflow_types",
                ["workflow_type_key"],
                ["workflow_type_key"],
                ondelete="RESTRICT",
            )


def downgrade() -> None:
    op.drop_constraint("fk_workflow_executions_workflow_type_key", "workflow_executions", type_="foreignkey")
    op.drop_index("ix_workflow_executions_workflow_type_key", table_name="workflow_executions")
    op.drop_column("workflow_executions", "workflow_type_key")
    op.drop_index("ix_workflow_type_operations_workflow_type_key", table_name="workflow_type_operations")
    op.drop_table("workflow_type_operations")
    op.drop_table("workflow_types")
