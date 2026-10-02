"""project automation optional delivery_text_channel_id

Revision ID: 20260330_0050
Revises: 20260328_0049
Create Date: 2026-03-30 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260330_0050"
down_revision = "20260328_0049"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table_name in inspector.get_table_names()


def _column_exists(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(
        col.get("name") == column_name for col in inspector.get_columns(table_name)
    )


def upgrade() -> None:
    if not _table_exists("project_automations") or not _column_exists(
        "project_automations", "delivery_text_channel_id"
    ):
        return
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"
    with op.batch_alter_table(
        "project_automations", recreate="auto" if is_sqlite else "never"
    ) as batch_op:
        batch_op.alter_column(
            "delivery_text_channel_id",
            existing_type=sa.String(length=64),
            nullable=True,
            existing_nullable=False,
        )


def downgrade() -> None:
    if not _table_exists("project_automations") or not _column_exists(
        "project_automations", "delivery_text_channel_id"
    ):
        return
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"
    # SQLite cannot easily enforce NOT NULL if nulls exist; best-effort for Postgres.
    with op.batch_alter_table(
        "project_automations", recreate="auto" if is_sqlite else "never"
    ) as batch_op:
        batch_op.alter_column(
            "delivery_text_channel_id",
            existing_type=sa.String(length=64),
            nullable=False,
            existing_nullable=True,
        )
