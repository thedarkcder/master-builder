"""run issue summary and description context

Revision ID: 20260207_0006
Revises: 20260207_0005
Create Date: 2026-02-07 00:30:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260207_0006"
down_revision = "20260207_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("issue_summary", sa.Text(), nullable=True))
    op.add_column("runs", sa.Column("issue_description", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "issue_description")
    op.drop_column("runs", "issue_summary")
