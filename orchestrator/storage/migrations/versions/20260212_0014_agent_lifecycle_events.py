"""add shared agent lifecycle event storage

Revision ID: 20260212_0014
Revises: 20260211_0013
Create Date: 2026-02-12 17:40:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260212_0014"
down_revision = "20260211_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_lifecycle_events",
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("project_id", sa.String(length=128), nullable=True),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("issue_key", sa.String(length=64), nullable=True),
        sa.Column("agent_id", sa.String(length=128), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.project_id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index("ix_agent_lifecycle_events_tenant_id", "agent_lifecycle_events", ["tenant_id"], unique=False)
    op.create_index("ix_agent_lifecycle_events_project_id", "agent_lifecycle_events", ["project_id"], unique=False)
    op.create_index("ix_agent_lifecycle_events_run_id", "agent_lifecycle_events", ["run_id"], unique=False)
    op.create_index("ix_agent_lifecycle_events_agent_id", "agent_lifecycle_events", ["agent_id"], unique=False)
    op.create_index("ix_agent_lifecycle_events_event_type", "agent_lifecycle_events", ["event_type"], unique=False)
    op.create_index("ix_agent_lifecycle_events_recorded_at", "agent_lifecycle_events", ["recorded_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_agent_lifecycle_events_recorded_at", table_name="agent_lifecycle_events")
    op.drop_index("ix_agent_lifecycle_events_event_type", table_name="agent_lifecycle_events")
    op.drop_index("ix_agent_lifecycle_events_agent_id", table_name="agent_lifecycle_events")
    op.drop_index("ix_agent_lifecycle_events_run_id", table_name="agent_lifecycle_events")
    op.drop_index("ix_agent_lifecycle_events_project_id", table_name="agent_lifecycle_events")
    op.drop_index("ix_agent_lifecycle_events_tenant_id", table_name="agent_lifecycle_events")
    op.drop_table("agent_lifecycle_events")
