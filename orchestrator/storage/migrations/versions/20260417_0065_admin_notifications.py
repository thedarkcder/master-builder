"""add admin notifications

Revision ID: 20260417_0065
Revises: 20260413_0064
Create Date: 2026-04-17 11:20:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260417_0065"
down_revision = "20260413_0064"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_notifications",
        sa.Column("notification_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=255), nullable=True),
        sa.Column("project_id", sa.String(length=255), nullable=True),
        sa.Column("scope_type", sa.String(length=64), nullable=False),
        sa.Column("scope_id", sa.String(length=255), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=32), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("action_label", sa.String(length=255), nullable=True),
        sa.Column("action_path", sa.Text(), nullable=True),
        sa.Column("fingerprint", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("context_json", sa.JSON(), nullable=False),
        sa.Column("first_emitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_emitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("notification_id"),
        sa.UniqueConstraint("fingerprint", name="uq_admin_notifications_fingerprint"),
    )
    op.create_index(
        "ix_admin_notifications_status_last_emitted_at",
        "admin_notifications",
        ["status", "last_emitted_at"],
    )
    op.create_index(
        "ix_admin_notifications_scope_type_scope_id",
        "admin_notifications",
        ["scope_type", "scope_id"],
    )
    op.create_index(
        "ix_admin_notifications_tenant_id_status",
        "admin_notifications",
        ["tenant_id", "status"],
    )
    op.create_index(
        "ix_admin_notifications_kind_status",
        "admin_notifications",
        ["kind", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_admin_notifications_kind_status", table_name="admin_notifications")
    op.drop_index("ix_admin_notifications_tenant_id_status", table_name="admin_notifications")
    op.drop_index("ix_admin_notifications_scope_type_scope_id", table_name="admin_notifications")
    op.drop_index("ix_admin_notifications_status_last_emitted_at", table_name="admin_notifications")
    op.drop_table("admin_notifications")
