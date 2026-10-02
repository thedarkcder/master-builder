"""add internal coolify deployment releases

Revision ID: 20260409_0056
Revises: 20260409_0055
Create Date: 2026-04-09 13:15:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260507_0108"
down_revision = "20260507_0107"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table_name in inspector.get_table_names()


def _index_exists(table_name: str, index_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return False
    return any(
        index.get("name") == index_name for index in inspector.get_indexes(table_name)
    )


def upgrade() -> None:
    if not _table_exists("project_deployment_releases"):
        op.create_table(
            "project_deployment_releases",
            sa.Column("release_id", sa.String(length=64), nullable=False),
            sa.Column("tenant_id", sa.String(length=128), nullable=False),
            sa.Column("project_id", sa.String(length=128), nullable=False),
            sa.Column("provider", sa.String(length=64), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("environment_name", sa.String(length=128), nullable=True),
            sa.Column("source_strategy", sa.String(length=64), nullable=True),
            sa.Column("git_ref", sa.String(length=255), nullable=True),
            sa.Column("commit_sha", sa.String(length=64), nullable=True),
            sa.Column("requested_by_user_id", sa.String(length=64), nullable=True),
            sa.Column(
                "deployment_snapshot",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            ),
            sa.Column(
                "provider_context",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            ),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["project_id"], ["projects.project_id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("release_id"),
        )

    for index_name, columns in (
        ("ix_project_deployment_releases_tenant_id", ["tenant_id"]),
        ("ix_project_deployment_releases_project_id", ["project_id"]),
        ("ix_project_deployment_releases_status", ["status"]),
        (
            "ix_project_deployment_releases_tenant_project_created_at",
            ["tenant_id", "project_id", "created_at"],
        ),
        ("ix_project_deployment_releases_project_status", ["project_id", "status"]),
    ):
        if not _index_exists("project_deployment_releases", index_name):
            op.create_index(
                index_name, "project_deployment_releases", columns, unique=False
            )


def downgrade() -> None:
    if _table_exists("project_deployment_releases"):
        for index_name in (
            "ix_project_deployment_releases_project_status",
            "ix_project_deployment_releases_tenant_project_created_at",
            "ix_project_deployment_releases_status",
            "ix_project_deployment_releases_project_id",
            "ix_project_deployment_releases_tenant_id",
        ):
            if _index_exists("project_deployment_releases", index_name):
                op.drop_index(index_name, table_name="project_deployment_releases")
        op.drop_table("project_deployment_releases")
