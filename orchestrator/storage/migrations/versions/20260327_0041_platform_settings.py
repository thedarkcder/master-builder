"""add platform settings table

Revision ID: 20260327_0041
Revises: 20260327_0040
Create Date: 2026-03-27
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260327_0041"
down_revision = "20260327_0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "platform_settings",
        sa.Column("setting_key", sa.String(length=128), nullable=False),
        sa.Column("value_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("setting_key"),
    )


def downgrade() -> None:
    op.drop_table("platform_settings")
