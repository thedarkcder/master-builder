"""repair missing projects.discord_config after 0010 drift

Revision ID: 20260209_0011
Revises: 20260209_0010
Create Date: 2026-02-09 19:05:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260209_0011"
down_revision = "20260209_0010"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return any(column.get("name") == column_name for column in inspector.get_columns(table_name))


def upgrade() -> None:
    if not _has_column("projects", "discord_config"):
        with op.batch_alter_table("projects", schema=None) as batch_op:
            batch_op.add_column(
                sa.Column("discord_config", sa.JSON(), nullable=False, server_default=sa.text("'{}'"))
            )

    bind = op.get_bind()
    projects = sa.table(
        "projects",
        sa.column("project_id", sa.String()),
        sa.column("tenant_id", sa.String()),
        sa.column("discord_config", sa.JSON()),
    )
    tenants = sa.table(
        "tenants",
        sa.column("tenant_id", sa.String()),
        sa.column("discord_config", sa.JSON()),
    )

    rows = bind.execute(
        sa.select(
            projects.c.project_id,
            projects.c.discord_config,
            tenants.c.discord_config,
        ).select_from(
            projects.join(tenants, projects.c.tenant_id == tenants.c.tenant_id)
        )
    ).all()
    for project_id, project_discord_config, tenant_discord_config in rows:
        if isinstance(project_discord_config, dict) and project_discord_config:
            continue
        if not isinstance(tenant_discord_config, dict):
            continue
        merged: dict[str, object] = {}
        for key in (
            "channel_id",
            "notify_events",
            "ask_thread_channel_ids",
            "seed_followup_thread_channel_ids",
            "allowed_user_ids",
            "allowlist_requests",
        ):
            value = tenant_discord_config.get(key)
            if value in (None, "", []):
                continue
            merged[key] = value
        if not merged:
            continue
        bind.execute(
            projects.update()
            .where(projects.c.project_id == project_id)
            .values(discord_config=merged)
        )


def downgrade() -> None:
    # Repair migration is intentionally non-destructive on downgrade.
    pass

