"""add run preview deployment release metadata

Revision ID: 20260530_0117
Revises: 20260527_0116
Create Date: 2026-05-30 02:15:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260530_0117"
down_revision = "20260527_0116"
branch_labels = None
depends_on = None


def _column_exists(table_name: str, column_name: str) -> bool:
    return column_name in {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def _index_exists(table_name: str, index_name: str) -> bool:
    return index_name in {
        index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table_name)
    }


def upgrade() -> None:
    if not _column_exists("project_deployment_releases", "release_kind"):
        op.add_column(
            "project_deployment_releases",
            sa.Column(
                "release_kind",
                sa.String(length=32),
                nullable=False,
                server_default="production",
            ),
        )
    if not _column_exists("project_deployment_releases", "source_run_id"):
        op.add_column(
            "project_deployment_releases",
            sa.Column("source_run_id", sa.String(length=64), nullable=True),
        )
    if not _column_exists("project_deployment_releases", "pr_number"):
        op.add_column(
            "project_deployment_releases",
            sa.Column("pr_number", sa.Integer(), nullable=True),
        )
    if not _column_exists("project_deployment_releases", "delivery_metadata"):
        op.add_column(
            "project_deployment_releases",
            sa.Column(
                "delivery_metadata",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            ),
        )
    if not _column_exists("project_deployment_releases", "destroyed_at"):
        op.add_column(
            "project_deployment_releases",
            sa.Column("destroyed_at", sa.DateTime(timezone=True), nullable=True),
        )

    if not _index_exists(
        "project_deployment_releases", "ix_project_deployment_releases_release_kind"
    ):
        op.create_index(
            "ix_project_deployment_releases_release_kind",
            "project_deployment_releases",
            ["release_kind"],
            unique=False,
        )
    if not _index_exists(
        "project_deployment_releases", "ix_project_deployment_releases_source_run_id"
    ):
        op.create_index(
            "ix_project_deployment_releases_source_run_id",
            "project_deployment_releases",
            ["source_run_id"],
            unique=False,
        )
    if not _index_exists(
        "project_deployment_releases", "ix_project_deployment_releases_pr_number"
    ):
        op.create_index(
            "ix_project_deployment_releases_pr_number",
            "project_deployment_releases",
            ["pr_number"],
            unique=False,
        )
    if not _index_exists(
        "project_deployment_releases",
        "ix_project_deployment_releases_project_kind_status",
    ):
        op.create_index(
            "ix_project_deployment_releases_project_kind_status",
            "project_deployment_releases",
            ["project_id", "release_kind", "status"],
            unique=False,
        )
    if not _index_exists(
        "project_deployment_releases",
        "ix_project_deployment_releases_project_pr_number",
    ):
        op.create_index(
            "ix_project_deployment_releases_project_pr_number",
            "project_deployment_releases",
            ["project_id", "pr_number"],
            unique=False,
        )

    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE project_deployment_releases SET release_kind = 'production' WHERE release_kind IS NULL OR release_kind = ''"
        )
    )
    bind.execute(
        sa.text(
            "UPDATE project_deployment_releases SET delivery_metadata = '{}' WHERE delivery_metadata IS NULL"
        )
    )


def downgrade() -> None:
    for index_name in (
        "ix_project_deployment_releases_project_pr_number",
        "ix_project_deployment_releases_project_kind_status",
        "ix_project_deployment_releases_pr_number",
        "ix_project_deployment_releases_source_run_id",
        "ix_project_deployment_releases_release_kind",
    ):
        if _index_exists("project_deployment_releases", index_name):
            op.drop_index(index_name, table_name="project_deployment_releases")
    for column_name in (
        "destroyed_at",
        "delivery_metadata",
        "pr_number",
        "source_run_id",
        "release_kind",
    ):
        if _column_exists("project_deployment_releases", column_name):
            op.drop_column("project_deployment_releases", column_name)
