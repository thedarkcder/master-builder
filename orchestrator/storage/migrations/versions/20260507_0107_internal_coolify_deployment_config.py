"""add internal coolify deployment config blobs

Revision ID: 20260409_0055
Revises: 20260407_0054
Create Date: 2026-04-09 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260507_0107"
down_revision = "20260505_0106"
branch_labels = None
depends_on = None


def _column_exists(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(
        column.get("name") == column_name
        for column in inspector.get_columns(table_name)
    )


def upgrade() -> None:
    if not _column_exists("tenants", "deployment_plane_config"):
        with op.batch_alter_table("tenants", recreate="auto") as batch_op:
            batch_op.add_column(
                sa.Column(
                    "deployment_plane_config",
                    sa.JSON(),
                    nullable=False,
                    server_default=sa.text("'{}'"),
                )
            )

    if not _column_exists("projects", "deployment_config"):
        with op.batch_alter_table("projects", recreate="auto") as batch_op:
            batch_op.add_column(
                sa.Column(
                    "deployment_config",
                    sa.JSON(),
                    nullable=False,
                    server_default=sa.text("'{}'"),
                )
            )


def downgrade() -> None:
    if _column_exists("projects", "deployment_config"):
        with op.batch_alter_table("projects", recreate="auto") as batch_op:
            batch_op.drop_column("deployment_config")

    if _column_exists("tenants", "deployment_plane_config"):
        with op.batch_alter_table("tenants", recreate="auto") as batch_op:
            batch_op.drop_column("deployment_plane_config")
