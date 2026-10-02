"""add shared followup context table

Revision ID: 20260323_0036
Revises: 20260323_0035
Create Date: 2026-03-23
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260323_0036"
down_revision = "20260323_0035"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table_name in inspector.get_table_names()


def _index_exists(table_name: str, index_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(
        index.get("name") == index_name for index in inspector.get_indexes(table_name)
    )


def upgrade() -> None:
    if not _table_exists("followup_contexts"):
        op.create_table(
            "followup_contexts",
            sa.Column("context_id", sa.String(length=64), nullable=False),
            sa.Column("tenant_id", sa.String(length=128), nullable=False),
            sa.Column("project_id", sa.String(length=128), nullable=True),
            sa.Column("context_type", sa.String(length=64), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("channel_id", sa.String(length=64), nullable=True),
            sa.Column("thread_channel_id", sa.String(length=64), nullable=True),
            sa.Column("root_message_id", sa.String(length=64), nullable=True),
            sa.Column("issue_key", sa.String(length=64), nullable=True),
            sa.Column("request_id", sa.String(length=64), nullable=True),
            sa.Column("run_id", sa.String(length=64), nullable=True),
            sa.Column("metadata_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(
                ["project_id"], ["projects.project_id"], ondelete="SET NULL"
            ),
            sa.ForeignKeyConstraint(["run_id"], ["runs.run_id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(
                ["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("context_id"),
        )
    for index_name, columns in (
        (
            "ix_followup_contexts_tenant_status_thread",
            ["tenant_id", "status", "thread_channel_id"],
        ),
        (
            "ix_followup_contexts_tenant_status_channel",
            ["tenant_id", "status", "channel_id"],
        ),
        (
            "ix_followup_contexts_tenant_status_root_message",
            ["tenant_id", "status", "root_message_id"],
        ),
        (
            "ix_followup_contexts_tenant_status_request",
            ["tenant_id", "status", "request_id"],
        ),
        (
            "ix_followup_contexts_tenant_type_issue",
            ["tenant_id", "context_type", "issue_key"],
        ),
        ("ix_followup_contexts_created_at", ["created_at"]),
    ):
        if not _index_exists("followup_contexts", index_name):
            op.create_index(index_name, "followup_contexts", columns, unique=False)


def downgrade() -> None:
    if not _table_exists("followup_contexts"):
        return
    for index_name in (
        "ix_followup_contexts_created_at",
        "ix_followup_contexts_tenant_type_issue",
        "ix_followup_contexts_tenant_status_request",
        "ix_followup_contexts_tenant_status_root_message",
        "ix_followup_contexts_tenant_status_channel",
        "ix_followup_contexts_tenant_status_thread",
    ):
        if _index_exists("followup_contexts", index_name):
            op.drop_index(index_name, table_name="followup_contexts")
    op.drop_table("followup_contexts")
