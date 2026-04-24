"""Rename workflow execution identity from issue to source reference.

Revision ID: 20260424_0091
Revises: 20260422_0090
Create Date: 2026-04-24 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = "20260424_0091"
down_revision = "20260422_0090"
branch_labels = None
depends_on = None


ACTIVE_WORKFLOW_STATUS_FILTER = "status IN ('queued', 'running', 'waiting_for_input')"


def upgrade() -> None:
    with op.batch_alter_table("workflow_executions") as batch_op:
        batch_op.drop_index("ix_workflow_executions_issue_key")
        batch_op.drop_index("uq_workflow_executions_active_scope")
        batch_op.add_column(sa.Column("source_system", sa.String(length=64), nullable=False, server_default="jira"))
        batch_op.alter_column("issue_key", new_column_name="source_ref", existing_type=sa.String(length=64), type_=sa.String(length=255), nullable=False)
        batch_op.alter_column("issue_summary", new_column_name="display_name", existing_type=sa.Text(), nullable=True)
        batch_op.alter_column("issue_description", new_column_name="source_description", existing_type=sa.Text(), nullable=True)
    op.create_index("ix_workflow_executions_source", "workflow_executions", ["source_system", "source_ref"], unique=False)
    op.create_index(
        "uq_workflow_executions_active_scope",
        "workflow_executions",
        ["tenant_id", "source_system", "source_ref", "dedupe_scope"],
        unique=True,
        postgresql_where=sa.text(ACTIVE_WORKFLOW_STATUS_FILTER),
        sqlite_where=sa.text(ACTIVE_WORKFLOW_STATUS_FILTER),
    )


def downgrade() -> None:
    op.drop_index("uq_workflow_executions_active_scope", table_name="workflow_executions")
    op.drop_index("ix_workflow_executions_source", table_name="workflow_executions")
    with op.batch_alter_table("workflow_executions") as batch_op:
        batch_op.alter_column("source_ref", new_column_name="issue_key", existing_type=sa.String(length=255), type_=sa.String(length=64), nullable=False)
        batch_op.alter_column("display_name", new_column_name="issue_summary", existing_type=sa.Text(), nullable=True)
        batch_op.alter_column("source_description", new_column_name="issue_description", existing_type=sa.Text(), nullable=True)
        batch_op.drop_column("source_system")
    op.create_index("ix_workflow_executions_issue_key", "workflow_executions", ["issue_key"], unique=False)
    op.create_index(
        "uq_workflow_executions_active_scope",
        "workflow_executions",
        ["tenant_id", "issue_key", "dedupe_scope"],
        unique=True,
        postgresql_where=sa.text(ACTIVE_WORKFLOW_STATUS_FILTER),
        sqlite_where=sa.text(ACTIVE_WORKFLOW_STATUS_FILTER),
    )
