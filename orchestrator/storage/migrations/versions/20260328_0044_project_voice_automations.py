"""add project voice automations

Revision ID: 20260328_0044
Revises: 20260328_0043
Create Date: 2026-03-28 15:30:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260328_0044"
down_revision = "20260328_0043"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table_name in inspector.get_table_names()


def _index_exists(table_name: str, index_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(index.get("name") == index_name for index in inspector.get_indexes(table_name))


def upgrade() -> None:
    if not _table_exists("project_automations"):
        op.create_table(
        "project_automations",
        sa.Column("automation_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("project_id", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("timezone", sa.String(length=128), nullable=False),
        sa.Column("days_of_week", sa.JSON(), nullable=False),
        sa.Column("local_time", sa.String(length=8), nullable=False),
        sa.Column("delivery_text_channel_id", sa.String(length=64), nullable=False),
        sa.Column("voice_id", sa.String(length=64), nullable=True),
        sa.Column("fallback_lookback_hours", sa.Integer(), nullable=False, server_default="24"),
        sa.Column("last_successful_window_end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.project_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("automation_id"),
        sa.UniqueConstraint("tenant_id", "project_id", "kind", name="uq_project_automations_scope_kind"),
        )
    for index_name, columns in (
        ("ix_project_automations_due_scan", ["enabled", "next_run_at"]),
        ("ix_project_automations_tenant_id", ["tenant_id"]),
        ("ix_project_automations_project_id", ["project_id"]),
        ("ix_project_automations_kind", ["kind"]),
        ("ix_project_automations_last_successful_window_end_at", ["last_successful_window_end_at"]),
        ("ix_project_automations_next_run_at", ["next_run_at"]),
    ):
        if not _index_exists("project_automations", index_name):
            op.create_index(index_name, "project_automations", columns, unique=False)

    if not _table_exists("project_automation_executions"):
        op.create_table(
        "project_automation_executions",
        sa.Column("execution_id", sa.String(length=64), nullable=False),
        sa.Column("automation_id", sa.String(length=64), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("dedupe_key", sa.String(length=255), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("discord_message_id", sa.String(length=128), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["automation_id"], ["project_automations.automation_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("execution_id"),
        sa.UniqueConstraint("automation_id", "scheduled_for", name="uq_project_automation_executions_automation_scheduled_for"),
        sa.UniqueConstraint("dedupe_key", name="uq_project_automation_executions_dedupe_key"),
        )
    for index_name, columns in (
        ("ix_project_automation_executions_due_scan", ["status", "scheduled_for"]),
        ("ix_project_automation_executions_automation_history", ["automation_id", "scheduled_for"]),
        ("ix_project_automation_executions_automation_id", ["automation_id"]),
        ("ix_project_automation_executions_status", ["status"]),
    ):
        if not _index_exists("project_automation_executions", index_name):
            op.create_index(index_name, "project_automation_executions", columns, unique=False)


def downgrade() -> None:
    if _table_exists("project_automation_executions"):
        for index_name in (
            "ix_project_automation_executions_status",
            "ix_project_automation_executions_automation_id",
            "ix_project_automation_executions_automation_history",
            "ix_project_automation_executions_due_scan",
        ):
            if _index_exists("project_automation_executions", index_name):
                op.drop_index(index_name, table_name="project_automation_executions")
        op.drop_table("project_automation_executions")

    if _table_exists("project_automations"):
        for index_name in (
            "ix_project_automations_next_run_at",
            "ix_project_automations_last_successful_window_end_at",
            "ix_project_automations_kind",
            "ix_project_automations_project_id",
            "ix_project_automations_tenant_id",
            "ix_project_automations_due_scan",
        ):
            if _index_exists("project_automations", index_name):
                op.drop_index(index_name, table_name="project_automations")
        op.drop_table("project_automations")
