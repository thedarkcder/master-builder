"""add discord command sync runtime status table

Revision ID: 20260322_0033
Revises: 20260321_0032
Create Date: 2026-03-22 10:30:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260322_0033"
down_revision = "20260321_0032"
branch_labels = None
depends_on = None


def _has_table(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def _has_index(table_name: str, index_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return any(
        index.get("name") == index_name for index in inspector.get_indexes(table_name)
    )


def upgrade() -> None:
    if not _has_table("discord_command_sync_runtime_states"):
        op.create_table(
            "discord_command_sync_runtime_states",
            sa.Column("runtime_name", sa.String(length=64), nullable=False),
            sa.Column("synced", sa.Boolean(), nullable=False),
            sa.Column("healthy", sa.Boolean(), nullable=False),
            sa.Column("interaction_ingress_ready", sa.Boolean(), nullable=False),
            sa.Column("bot_token_configured", sa.Boolean(), nullable=False),
            sa.Column("guild_id_configured", sa.Boolean(), nullable=False),
            sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_failure_reason", sa.String(length=64), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("guild_id", sa.String(length=128), nullable=True),
            sa.Column("application_id", sa.String(length=128), nullable=True),
            sa.Column("command_count", sa.Integer(), nullable=False),
            sa.Column("service_instance_id", sa.String(length=128), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("runtime_name"),
        )
    if not _has_index(
        "discord_command_sync_runtime_states",
        "ix_discord_command_sync_runtime_states_updated_at",
    ):
        op.create_index(
            "ix_discord_command_sync_runtime_states_updated_at",
            "discord_command_sync_runtime_states",
            ["updated_at"],
            unique=False,
        )


def downgrade() -> None:
    if _has_table("discord_command_sync_runtime_states"):
        if _has_index(
            "discord_command_sync_runtime_states",
            "ix_discord_command_sync_runtime_states_updated_at",
        ):
            op.drop_index(
                "ix_discord_command_sync_runtime_states_updated_at",
                table_name="discord_command_sync_runtime_states",
            )
        op.drop_table("discord_command_sync_runtime_states")
