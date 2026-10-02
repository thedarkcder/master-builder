"""add explicit followup context identity fields

Revision ID: 20260328_0047
Revises: 20260328_0046
Create Date: 2026-03-28
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260328_0047"
down_revision = "20260328_0046"
branch_labels = None
depends_on = None


def _column_names(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {
        str(column.get("name") or "") for column in inspector.get_columns(table_name)
    }


def _index_exists(table_name: str, index_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(
        index.get("name") == index_name for index in inspector.get_indexes(table_name)
    )


def upgrade() -> None:
    column_names = _column_names("followup_contexts")
    if "owner_user_id" not in column_names:
        op.add_column(
            "followup_contexts",
            sa.Column("owner_user_id", sa.String(length=64), nullable=True),
        )
    if "origin_command" not in column_names:
        op.add_column(
            "followup_contexts",
            sa.Column("origin_command", sa.String(length=64), nullable=True),
        )
    if not _index_exists(
        "followup_contexts", "ix_followup_contexts_tenant_status_owner"
    ):
        op.create_index(
            "ix_followup_contexts_tenant_status_owner",
            "followup_contexts",
            ["tenant_id", "status", "owner_user_id"],
            unique=False,
        )


def downgrade() -> None:
    if _index_exists("followup_contexts", "ix_followup_contexts_tenant_status_owner"):
        op.drop_index(
            "ix_followup_contexts_tenant_status_owner", table_name="followup_contexts"
        )
    column_names = _column_names("followup_contexts")
    if "origin_command" in column_names:
        op.drop_column("followup_contexts", "origin_command")
    if "owner_user_id" in column_names:
        op.drop_column("followup_contexts", "owner_user_id")
