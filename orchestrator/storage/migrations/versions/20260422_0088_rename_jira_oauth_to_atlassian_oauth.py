"""Rename shared Jira OAuth storage to Atlassian OAuth.

Revision ID: 20260422_0088
Revises: 20260421_0087
Create Date: 2026-04-22 11:35:00.000000
"""

from __future__ import annotations

from alembic import op


revision = "20260422_0088"
down_revision = "20260421_0087"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.rename_table("jira_oauth_connections", "atlassian_oauth_connections")
    op.drop_index("ix_jira_oauth_connections_cloud_id", table_name="atlassian_oauth_connections")
    op.create_index(
        "ix_atlassian_oauth_connections_cloud_id",
        "atlassian_oauth_connections",
        ["cloud_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_atlassian_oauth_connections_cloud_id", table_name="atlassian_oauth_connections")
    op.rename_table("atlassian_oauth_connections", "jira_oauth_connections")
    op.create_index(
        "ix_jira_oauth_connections_cloud_id",
        "jira_oauth_connections",
        ["cloud_id"],
        unique=False,
    )
