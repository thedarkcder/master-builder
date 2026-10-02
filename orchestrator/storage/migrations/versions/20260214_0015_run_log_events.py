"""add run log events storage

Revision ID: 20260214_0015
Revises: 20260212_0014
Create Date: 2026-02-14 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260214_0015"
down_revision = "20260212_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "run_log_events",
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("project_id", sa.String(length=128), nullable=True),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("issue_key", sa.String(length=64), nullable=True),
        sa.Column("agent_id", sa.String(length=128), nullable=False),
        sa.Column("stage", sa.String(length=64), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=True),
        sa.Column("stream", sa.String(length=16), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.project_id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.run_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index(
        "ix_run_log_events_tenant_id", "run_log_events", ["tenant_id"], unique=False
    )
    op.create_index(
        "ix_run_log_events_project_id", "run_log_events", ["project_id"], unique=False
    )
    op.create_index(
        "ix_run_log_events_run_id", "run_log_events", ["run_id"], unique=False
    )
    op.create_index(
        "ix_run_log_events_agent_id", "run_log_events", ["agent_id"], unique=False
    )
    op.create_index(
        "ix_run_log_events_stage", "run_log_events", ["stage"], unique=False
    )
    op.create_index(
        "ix_run_log_events_recorded_at", "run_log_events", ["recorded_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_run_log_events_recorded_at", table_name="run_log_events")
    op.drop_index("ix_run_log_events_stage", table_name="run_log_events")
    op.drop_index("ix_run_log_events_agent_id", table_name="run_log_events")
    op.drop_index("ix_run_log_events_run_id", table_name="run_log_events")
    op.drop_index("ix_run_log_events_project_id", table_name="run_log_events")
    op.drop_index("ix_run_log_events_tenant_id", table_name="run_log_events")
    op.drop_table("run_log_events")
