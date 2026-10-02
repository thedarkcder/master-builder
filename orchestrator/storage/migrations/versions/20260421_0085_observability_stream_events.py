"""create observability stream events table

Revision ID: 20260421_0085
Revises: 20260420_0084
Create Date: 2026-04-21 14:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision = "20260421_0085"
down_revision = "20260420_0084"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "observability_stream_events" in inspector.get_table_names():
        return

    op.create_table(
        "observability_stream_events",
        sa.Column("stream_offset", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("project_id", sa.String(length=128), nullable=True),
        sa.Column("workflow_id", sa.String(length=64), nullable=True),
        sa.Column("run_id", sa.String(length=64), nullable=True),
        sa.Column("operation_id", sa.String(length=64), nullable=True),
        sa.Column("attempt_id", sa.String(length=64), nullable=True),
        sa.Column("issue_key", sa.String(length=64), nullable=True),
        sa.Column("event_kind", sa.String(length=64), nullable=False),
        sa.Column("level", sa.String(length=16), nullable=False),
        sa.Column("source_component", sa.String(length=128), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.project_id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["workflow_id"], ["workflow_executions.workflow_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.run_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["operation_id"], ["workflow_operations.operation_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("stream_offset"),
    )
    op.create_index(
        "ix_observability_stream_events_tenant_id_stream_offset",
        "observability_stream_events",
        ["tenant_id", "stream_offset"],
        unique=False,
    )
    op.create_index(
        "ix_observability_stream_events_operation_id_stream_offset",
        "observability_stream_events",
        ["operation_id", "stream_offset"],
        unique=False,
    )
    op.create_index(
        "ix_observability_stream_events_attempt_id_stream_offset",
        "observability_stream_events",
        ["attempt_id", "stream_offset"],
        unique=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "observability_stream_events" not in inspector.get_table_names():
        return
    op.drop_index(
        "ix_observability_stream_events_attempt_id_stream_offset",
        table_name="observability_stream_events",
    )
    op.drop_index(
        "ix_observability_stream_events_operation_id_stream_offset",
        table_name="observability_stream_events",
    )
    op.drop_index(
        "ix_observability_stream_events_tenant_id_stream_offset",
        table_name="observability_stream_events",
    )
    op.drop_table("observability_stream_events")
