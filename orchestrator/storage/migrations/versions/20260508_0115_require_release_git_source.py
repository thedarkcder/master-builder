"""require immutable git source for deployment releases

Revision ID: 20260508_0115
Revises: 20260508_0114
Create Date: 2026-05-08 15:30:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260508_0115"
down_revision = "20260508_0114"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table_name in inspector.get_table_names()


def _column_exists(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return False
    return any(
        column["name"] == column_name for column in inspector.get_columns(table_name)
    )


def upgrade() -> None:
    if not _table_exists("project_deployment_releases"):
        return
    if not _column_exists(
        "project_deployment_releases", "git_ref"
    ) or not _column_exists(
        "project_deployment_releases",
        "commit_sha",
    ):
        return

    # Invalid historical rows predate the immutable source contract. They cannot be
    # shown as real deployments because the exact branch+commit is unknown.
    op.execute(
        sa.text(
            """
            DELETE FROM project_deployment_releases
            WHERE git_ref IS NULL
               OR TRIM(git_ref) = ''
               OR commit_sha IS NULL
               OR TRIM(commit_sha) = ''
            """
        )
    )

    with op.batch_alter_table("project_deployment_releases", schema=None) as batch_op:
        batch_op.alter_column(
            "git_ref", existing_type=sa.String(length=255), nullable=False
        )
        batch_op.alter_column(
            "commit_sha", existing_type=sa.String(length=64), nullable=False
        )


def downgrade() -> None:
    if not _table_exists("project_deployment_releases"):
        return
    if _column_exists("project_deployment_releases", "git_ref") and _column_exists(
        "project_deployment_releases",
        "commit_sha",
    ):
        with op.batch_alter_table(
            "project_deployment_releases", schema=None
        ) as batch_op:
            batch_op.alter_column(
                "git_ref", existing_type=sa.String(length=255), nullable=True
            )
            batch_op.alter_column(
                "commit_sha", existing_type=sa.String(length=64), nullable=True
            )
