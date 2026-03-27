"""add run stream events journal

Revision ID: 20260327_0040
Revises: 20260327_0039
Create Date: 2026-03-27 20:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260327_0040"
down_revision = "20260327_0039"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table_name in inspector.get_table_names()


def _has_index(table_name: str, index_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(index.get("name") == index_name for index in inspector.get_indexes(table_name))


def upgrade() -> None:
    if not _table_exists("run_stream_events"):
        op.create_table(
            "run_stream_events",
            sa.Column("stream_offset", sa.Integer(), nullable=False, autoincrement=True),
            sa.Column("event_kind", sa.String(length=32), nullable=False),
            sa.Column("tenant_id", sa.String(length=128), nullable=False),
            sa.Column("project_id", sa.String(length=128), nullable=True),
            sa.Column("run_id", sa.String(length=64), nullable=True),
            sa.Column("issue_key", sa.String(length=64), nullable=True),
            sa.Column("agent_id", sa.String(length=128), nullable=True),
            sa.Column("event_type", sa.String(length=64), nullable=True),
            sa.Column("invocation_id", sa.String(length=64), nullable=True),
            sa.Column("channel", sa.String(length=64), nullable=True),
            sa.Column("command", sa.String(length=128), nullable=True),
            sa.Column("working_dir", sa.String(length=1024), nullable=True),
            sa.Column("stage", sa.String(length=64), nullable=True),
            sa.Column("attempt", sa.Integer(), nullable=True),
            sa.Column("stream", sa.String(length=16), nullable=True),
            sa.Column("message", sa.Text(), nullable=True),
            sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["project_id"], ["projects.project_id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["run_id"], ["runs.run_id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("stream_offset"),
        )

    for index_name, columns in (
        ("ix_run_stream_events_event_kind", ["event_kind"]),
        ("ix_run_stream_events_tenant_id", ["tenant_id"]),
        ("ix_run_stream_events_project_id", ["project_id"]),
        ("ix_run_stream_events_run_id", ["run_id"]),
        ("ix_run_stream_events_agent_id", ["agent_id"]),
        ("ix_run_stream_events_event_type", ["event_type"]),
        ("ix_run_stream_events_invocation_id", ["invocation_id"]),
        ("ix_run_stream_events_channel", ["channel"]),
        ("ix_run_stream_events_command", ["command"]),
        ("ix_run_stream_events_stage", ["stage"]),
        ("ix_run_stream_events_recorded_at", ["recorded_at"]),
        ("ix_run_stream_events_run_id_stream_offset", ["run_id", "stream_offset"]),
        ("ix_run_stream_events_tenant_id_stream_offset", ["tenant_id", "stream_offset"]),
    ):
        if not _has_index("run_stream_events", index_name):
            op.create_index(index_name, "run_stream_events", columns, unique=False)


def downgrade() -> None:
    if not _table_exists("run_stream_events"):
        return
    for index_name in (
        "ix_run_stream_events_tenant_id_stream_offset",
        "ix_run_stream_events_run_id_stream_offset",
        "ix_run_stream_events_recorded_at",
        "ix_run_stream_events_stage",
        "ix_run_stream_events_command",
        "ix_run_stream_events_channel",
        "ix_run_stream_events_invocation_id",
        "ix_run_stream_events_event_type",
        "ix_run_stream_events_agent_id",
        "ix_run_stream_events_run_id",
        "ix_run_stream_events_project_id",
        "ix_run_stream_events_tenant_id",
        "ix_run_stream_events_event_kind",
    ):
        if _has_index("run_stream_events", index_name):
            op.drop_index(index_name, table_name="run_stream_events")
    op.drop_table("run_stream_events")
