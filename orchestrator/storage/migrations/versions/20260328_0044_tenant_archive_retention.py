"""add tenant archive retention timestamps

Revision ID: 20260328_0044
Revises: 20260327_0043
Create Date: 2026-03-28 10:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260328_0044"
down_revision = "20260327_0043"
branch_labels = None
depends_on = None


def _column_exists(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(
        column.get("name") == column_name
        for column in inspector.get_columns(table_name)
    )


def _has_index(table_name: str, index_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(
        index.get("name") == index_name for index in inspector.get_indexes(table_name)
    )


def upgrade() -> None:
    if not _column_exists("tenants", "archived_at"):
        op.add_column(
            "tenants",
            sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        )
    if not _column_exists("tenants", "purge_after_at"):
        op.add_column(
            "tenants",
            sa.Column("purge_after_at", sa.DateTime(timezone=True), nullable=True),
        )
    if not _has_index("tenants", "ix_tenants_archived_at"):
        op.create_index(
            "ix_tenants_archived_at", "tenants", ["archived_at"], unique=False
        )
    if not _has_index("tenants", "ix_tenants_purge_after_at"):
        op.create_index(
            "ix_tenants_purge_after_at", "tenants", ["purge_after_at"], unique=False
        )


def downgrade() -> None:
    if _has_index("tenants", "ix_tenants_purge_after_at"):
        op.drop_index("ix_tenants_purge_after_at", table_name="tenants")
    if _has_index("tenants", "ix_tenants_archived_at"):
        op.drop_index("ix_tenants_archived_at", table_name="tenants")
    if _column_exists("tenants", "purge_after_at"):
        op.drop_column("tenants", "purge_after_at")
    if _column_exists("tenants", "archived_at"):
        op.drop_column("tenants", "archived_at")
