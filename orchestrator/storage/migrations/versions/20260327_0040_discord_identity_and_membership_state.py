"""add discord identity links and membership discord state

Revision ID: 20260327_0040
Revises: 20260327_0039
Create Date: 2026-03-27
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260327_0040"
down_revision = "20260327_0039"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table_name in inspector.get_table_names()


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
    if not _column_exists("tenant_memberships", "discord_state"):
        with op.batch_alter_table("tenant_memberships", recreate="auto") as batch_op:
            batch_op.add_column(
                sa.Column(
                    "discord_state",
                    sa.JSON(),
                    nullable=False,
                    server_default=sa.text("'{}'"),
                )
            )

    if not _table_exists("tenant_user_discord_identities"):
        op.create_table(
            "tenant_user_discord_identities",
            sa.Column("user_id", sa.String(length=64), nullable=False),
            sa.Column("discord_user_id", sa.String(length=64), nullable=False),
            sa.Column("discord_username", sa.String(length=255), nullable=True),
            sa.Column("discord_global_name", sa.String(length=255), nullable=True),
            sa.Column("discord_avatar_hash", sa.String(length=255), nullable=True),
            sa.Column("linked_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["user_id"], ["tenant_users.user_id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("user_id"),
            sa.UniqueConstraint("discord_user_id"),
        )
    if not _has_index(
        "tenant_user_discord_identities",
        "ix_tenant_user_discord_identities_discord_user_id",
    ):
        op.create_index(
            "ix_tenant_user_discord_identities_discord_user_id",
            "tenant_user_discord_identities",
            ["discord_user_id"],
            unique=False,
        )


def downgrade() -> None:
    if _table_exists("tenant_user_discord_identities"):
        if _has_index(
            "tenant_user_discord_identities",
            "ix_tenant_user_discord_identities_discord_user_id",
        ):
            op.drop_index(
                "ix_tenant_user_discord_identities_discord_user_id",
                table_name="tenant_user_discord_identities",
            )
        op.drop_table("tenant_user_discord_identities")
    if _column_exists("tenant_memberships", "discord_state"):
        with op.batch_alter_table("tenant_memberships", recreate="auto") as batch_op:
            batch_op.drop_column("discord_state")
