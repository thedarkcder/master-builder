"""add pm interview cases table

Revision ID: 20260328_0043
Revises: 20260328_0042
Create Date: 2026-03-28
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260328_0043"
down_revision = "20260328_0042"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table_name in inspector.get_table_names()


def _index_exists(table_name: str, index_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(index.get("name") == index_name for index in inspector.get_indexes(table_name))


def upgrade() -> None:
    if not _table_exists("pm_interview_cases"):
        op.create_table(
            "pm_interview_cases",
            sa.Column("case_id", sa.String(length=64), nullable=False),
            sa.Column("tenant_id", sa.String(length=128), nullable=False),
            sa.Column("project_id", sa.String(length=128), nullable=True),
            sa.Column("request_id", sa.String(length=128), nullable=False),
            sa.Column("parent_issue_key", sa.String(length=64), nullable=True),
            sa.Column("source_kind", sa.String(length=64), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("channel_id", sa.String(length=64), nullable=False),
            sa.Column("thread_channel_id", sa.String(length=64), nullable=True),
            sa.Column("root_message_id", sa.String(length=64), nullable=True),
            sa.Column("owner_user_id", sa.String(length=64), nullable=True),
            sa.Column("source_text", sa.Text(), nullable=False),
            sa.Column("brief_json", sa.JSON(), nullable=False),
            sa.Column("evidence_json", sa.JSON(), nullable=False),
            sa.Column("question_history_json", sa.JSON(), nullable=False),
            sa.Column("current_question_json", sa.JSON(), nullable=False),
            sa.Column("next_question_json", sa.JSON(), nullable=False),
            sa.Column("missing_slots_json", sa.JSON(), nullable=False),
            sa.Column("notes_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["project_id"], ["projects.project_id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("case_id"),
            sa.UniqueConstraint("tenant_id", "request_id", name="uq_pm_interview_cases_tenant_request"),
        )

    index_specs = (
        ("ix_pm_interview_cases_tenant_status_channel", ["tenant_id", "status", "channel_id"]),
        ("ix_pm_interview_cases_tenant_status_thread", ["tenant_id", "status", "thread_channel_id"]),
        ("ix_pm_interview_cases_tenant_status_root_message", ["tenant_id", "status", "root_message_id"]),
        ("ix_pm_interview_cases_tenant_status_owner", ["tenant_id", "status", "owner_user_id"]),
        ("ix_pm_interview_cases_tenant_parent_issue", ["tenant_id", "parent_issue_key"]),
    )
    for index_name, columns in index_specs:
        if not _index_exists("pm_interview_cases", index_name):
            op.create_index(index_name, "pm_interview_cases", columns, unique=False)


def downgrade() -> None:
    if not _table_exists("pm_interview_cases"):
        return
    index_names = (
        "ix_pm_interview_cases_tenant_parent_issue",
        "ix_pm_interview_cases_tenant_status_owner",
        "ix_pm_interview_cases_tenant_status_root_message",
        "ix_pm_interview_cases_tenant_status_thread",
        "ix_pm_interview_cases_tenant_status_channel",
    )
    for index_name in index_names:
        if _index_exists("pm_interview_cases", index_name):
            op.drop_index(index_name, table_name="pm_interview_cases")
    op.drop_table("pm_interview_cases")
