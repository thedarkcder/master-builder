"""add pending human input requests for resumed runs

Revision ID: 20260313_0029
Revises: 20260313_0028
Create Date: 2026-03-13 19:30:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text


revision = "20260313_0029"
down_revision = "20260313_0028"
branch_labels = None
depends_on = None
_RUN_HUMAN_INPUT_REQUESTS_LOCK_KEY = 202603130029


def _has_table(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def _has_index(table_name: str, index_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return any(index.get("name") == index_name for index in inspector.get_indexes(table_name))


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table_name not in inspector.get_table_names():
        return False
    return any(column.get("name") == column_name for column in inspector.get_columns(table_name))


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": _RUN_HUMAN_INPUT_REQUESTS_LOCK_KEY},
        )
    if not _has_table("run_human_input_requests"):
        op.create_table(
            "run_human_input_requests",
            sa.Column("request_id", sa.String(length=64), nullable=False),
            sa.Column("tenant_id", sa.String(length=128), nullable=False),
            sa.Column("project_id", sa.String(length=128), nullable=True),
            sa.Column("source_run_id", sa.String(length=64), nullable=False),
            sa.Column("resumed_run_id", sa.String(length=64), nullable=True),
            sa.Column("issue_key", sa.String(length=64), nullable=False),
            sa.Column("source_stage", sa.String(length=64), nullable=False),
            sa.Column("resume_stage", sa.String(length=32), nullable=False),
            sa.Column("resume_session_id", sa.String(length=64), nullable=False),
            sa.Column("request_type", sa.String(length=64), nullable=False),
            sa.Column("prompt", sa.Text(), nullable=False),
            sa.Column("instructions", sa.Text(), nullable=True),
            sa.Column("expected_reply_format", sa.Text(), nullable=True),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("request_context_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("thread_channel_id", sa.String(length=64), nullable=True),
            sa.Column("thread_message_id", sa.String(length=64), nullable=True),
            sa.Column("answer_encrypted", sa.Text(), nullable=True),
            sa.Column("answer_source_ref", sa.String(length=128), nullable=True),
            sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["project_id"], ["projects.project_id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["source_run_id"], ["runs.run_id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["resumed_run_id"], ["runs.run_id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("request_id"),
        )

    for index_name, columns in (
        ("ix_run_human_input_requests_tenant_id", ["tenant_id"]),
        ("ix_run_human_input_requests_project_id", ["project_id"]),
        ("ix_run_human_input_requests_source_run_id", ["source_run_id"]),
        ("ix_run_human_input_requests_resumed_run_id", ["resumed_run_id"]),
        ("ix_run_human_input_requests_issue_key", ["issue_key"]),
        ("ix_run_human_input_requests_request_type", ["request_type"]),
        ("ix_run_human_input_requests_status", ["status"]),
        ("ix_run_human_input_requests_thread_channel_id", ["thread_channel_id"]),
        ("ix_run_human_input_requests_expires_at", ["expires_at"]),
        ("ix_run_human_input_requests_created_at", ["created_at"]),
    ):
        if all(_has_column("run_human_input_requests", column_name) for column_name in columns) and not _has_index(
            "run_human_input_requests", index_name
        ):
            op.create_index(index_name, "run_human_input_requests", columns, unique=False)


def downgrade() -> None:
    if not _has_table("run_human_input_requests"):
        return
    for index_name in (
        "ix_run_human_input_requests_created_at",
        "ix_run_human_input_requests_expires_at",
        "ix_run_human_input_requests_thread_channel_id",
        "ix_run_human_input_requests_status",
        "ix_run_human_input_requests_request_type",
        "ix_run_human_input_requests_issue_key",
        "ix_run_human_input_requests_resumed_run_id",
        "ix_run_human_input_requests_source_run_id",
        "ix_run_human_input_requests_project_id",
        "ix_run_human_input_requests_tenant_id",
    ):
        if _has_index("run_human_input_requests", index_name):
            op.drop_index(index_name, table_name="run_human_input_requests")
    op.drop_table("run_human_input_requests")
