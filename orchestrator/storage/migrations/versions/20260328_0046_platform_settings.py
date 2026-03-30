"""add platform settings table

Revision ID: 20260328_0046
Revises: 20260328_0045
Create Date: 2026-03-27
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260328_0046"
down_revision = "20260328_0045"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    if _table_exists("platform_settings"):
        return
    op.create_table(
        "platform_settings",
        sa.Column("setting_key", sa.String(length=128), nullable=False),
        sa.Column("value_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("setting_key"),
    )


def downgrade() -> None:
    if not _table_exists("platform_settings"):
        return
    op.drop_table("platform_settings")
