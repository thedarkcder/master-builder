"""create audit events table

Revision ID: 20260420_0084
Revises: 20260420_0083
Create Date: 2026-04-20 23:59:59.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision = "20260420_0084"
down_revision = "20260420_0083"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "audit_events" in inspector.get_table_names():
        return

    op.create_table(
        "audit_events",
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("project_id", sa.String(length=128), nullable=True),
        sa.Column("workflow_id", sa.String(length=64), nullable=True),
        sa.Column("run_id", sa.String(length=64), nullable=True),
        sa.Column("operation_id", sa.String(length=64), nullable=True),
        sa.Column("attempt_id", sa.String(length=64), nullable=True),
        sa.Column("issue_key", sa.String(length=64), nullable=True),
        sa.Column("actor_type", sa.String(length=64), nullable=True),
        sa.Column("actor_id", sa.String(length=128), nullable=True),
        sa.Column("source_component", sa.String(length=128), nullable=False),
        sa.Column("event_kind", sa.String(length=64), nullable=False),
        sa.Column("level", sa.String(length=16), nullable=False),
        sa.Column("correlation_id", sa.String(length=128), nullable=True),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("span_id", sa.String(length=64), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.project_id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workflow_id"], ["workflow_executions.workflow_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.run_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["operation_id"], ["workflow_operations.operation_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index("ix_audit_events_tenant_id_recorded_at", "audit_events", ["tenant_id", "recorded_at"], unique=False)
    op.create_index("ix_audit_events_project_id_recorded_at", "audit_events", ["project_id", "recorded_at"], unique=False)
    op.create_index("ix_audit_events_workflow_id_recorded_at", "audit_events", ["workflow_id", "recorded_at"], unique=False)
    op.create_index("ix_audit_events_run_id_recorded_at", "audit_events", ["run_id", "recorded_at"], unique=False)
    op.create_index("ix_audit_events_operation_id_recorded_at", "audit_events", ["operation_id", "recorded_at"], unique=False)
    op.create_index("ix_audit_events_event_kind", "audit_events", ["event_kind"], unique=False)
    op.create_index("ix_audit_events_level", "audit_events", ["level"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "audit_events" not in inspector.get_table_names():
        return
    op.drop_index("ix_audit_events_level", table_name="audit_events")
    op.drop_index("ix_audit_events_event_kind", table_name="audit_events")
    op.drop_index("ix_audit_events_operation_id_recorded_at", table_name="audit_events")
    op.drop_index("ix_audit_events_run_id_recorded_at", table_name="audit_events")
    op.drop_index("ix_audit_events_workflow_id_recorded_at", table_name="audit_events")
    op.drop_index("ix_audit_events_project_id_recorded_at", table_name="audit_events")
    op.drop_index("ix_audit_events_tenant_id_recorded_at", table_name="audit_events")
    op.drop_table("audit_events")
