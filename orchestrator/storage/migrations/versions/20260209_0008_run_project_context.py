"""add project context to runs

Revision ID: 20260209_0008
Revises: 20260208_0007
Create Date: 2026-02-09 09:25:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260209_0008"
down_revision = "20260208_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("project_id", sa.String(length=128), nullable=True))
        batch_op.create_index("ix_runs_project_id", ["project_id"], unique=False)
        batch_op.create_foreign_key(
            "fk_runs_project_id_projects",
            "projects",
            ["project_id"],
            ["project_id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.drop_constraint("fk_runs_project_id_projects", type_="foreignkey")
        batch_op.drop_index("ix_runs_project_id")
        batch_op.drop_column("project_id")
