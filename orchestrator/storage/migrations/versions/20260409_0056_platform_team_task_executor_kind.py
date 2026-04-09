"""add executor kind to platform team tasks

Revision ID: 20260409_0056
Revises: 20260409_0055
Create Date: 2026-04-09 15:40:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260409_0056"
down_revision = "20260409_0055"
branch_labels = None
depends_on = None


def _column_names(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    if "platform_team_tasks" in sa.inspect(op.get_bind()).get_table_names():
        columns = _column_names("platform_team_tasks")
        if "executor_kind" not in columns:
            op.add_column("platform_team_tasks", sa.Column("executor_kind", sa.String(length=128), nullable=True))


def downgrade() -> None:
    if "platform_team_tasks" in sa.inspect(op.get_bind()).get_table_names():
        columns = _column_names("platform_team_tasks")
        if "executor_kind" in columns:
            op.drop_column("platform_team_tasks", "executor_kind")
