"""jira oauth connections

Revision ID: 20260207_0004
Revises: 20260207_0003
Create Date: 2026-02-07 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260207_0004"
down_revision = "20260207_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "jira_oauth_connections",
        sa.Column("connection_id", sa.String(length=64), nullable=False),
        sa.Column("account_id", sa.String(length=255), nullable=False),
        sa.Column("account_email", sa.String(length=255), nullable=True),
        sa.Column("cloud_id", sa.String(length=128), nullable=False),
        sa.Column("site_url", sa.String(length=512), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("access_token_encrypted", sa.Text(), nullable=False),
        sa.Column("refresh_token_encrypted", sa.Text(), nullable=False),
        sa.Column("access_token_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("connection_id"),
    )
    op.create_index(
        "ix_jira_oauth_connections_cloud_id",
        "jira_oauth_connections",
        ["cloud_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_jira_oauth_connections_cloud_id", table_name="jira_oauth_connections")
    op.drop_table("jira_oauth_connections")
