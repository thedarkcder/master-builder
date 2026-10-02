"""Rename shared Jira OAuth storage to Atlassian OAuth.

Revision ID: 20260422_0088
Revises: 20260421_0087
Create Date: 2026-04-22 11:35:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260422_0088"
down_revision = "20260421_0087"
branch_labels = None
depends_on = None


def _has_table(table_name: str) -> bool:
    return table_name in sa.inspect(op.get_bind()).get_table_names()


def _has_index(table_name: str, index_name: str) -> bool:
    return index_name in {
        index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table_name)
    }


def upgrade() -> None:
    has_jira_table = _has_table("jira_oauth_connections")
    has_atlassian_table = _has_table("atlassian_oauth_connections")
    if has_jira_table and has_atlassian_table:
        raise RuntimeError("both Jira and Atlassian OAuth connection tables exist")
    if not has_jira_table and not has_atlassian_table:
        raise RuntimeError("missing Atlassian OAuth connection table")
    if has_jira_table:
        op.rename_table("jira_oauth_connections", "atlassian_oauth_connections")
    if _has_index("atlassian_oauth_connections", "ix_jira_oauth_connections_cloud_id"):
        op.drop_index(
            "ix_jira_oauth_connections_cloud_id",
            table_name="atlassian_oauth_connections",
        )
    if not _has_index(
        "atlassian_oauth_connections", "ix_atlassian_oauth_connections_cloud_id"
    ):
        op.create_index(
            "ix_atlassian_oauth_connections_cloud_id",
            "atlassian_oauth_connections",
            ["cloud_id"],
            unique=False,
        )


def downgrade() -> None:
    op.drop_index(
        "ix_atlassian_oauth_connections_cloud_id",
        table_name="atlassian_oauth_connections",
    )
    op.rename_table("atlassian_oauth_connections", "jira_oauth_connections")
    op.create_index(
        "ix_jira_oauth_connections_cloud_id",
        "jira_oauth_connections",
        ["cloud_id"],
        unique=False,
    )
