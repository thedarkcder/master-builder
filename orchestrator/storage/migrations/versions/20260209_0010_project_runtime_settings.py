"""add project runtime settings

Revision ID: 20260209_0010
Revises: 20260209_0009
Create Date: 2026-02-09 16:15:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260209_0010"
down_revision = "20260209_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("environment", sa.JSON(), nullable=False, server_default=sa.text("'{}'"))
        )
        batch_op.add_column(
            sa.Column("secret_refs", sa.JSON(), nullable=False, server_default=sa.text("'{}'"))
        )
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
            tenants.c.discord_config,
        ).select_from(
            projects.join(tenants, projects.c.tenant_id == tenants.c.tenant_id)
        )
    ).all()
    for project_id, tenant_discord_config in rows:
        if not isinstance(tenant_discord_config, dict):
            continue
        project_discord_config: dict[str, object] = {}
        for key in (
            "channel_id",
            "notify_events",
            "ask_thread_channel_ids",
            "seed_followup_thread_channel_ids",
        ):
            value = tenant_discord_config.get(key)
            if value in (None, "", []):
                continue
            project_discord_config[key] = value
        if project_discord_config:
            bind.execute(
                projects.update()
                .where(projects.c.project_id == project_id)
                .values(discord_config=project_discord_config)
            )


def downgrade() -> None:
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.drop_column("discord_config")
        batch_op.drop_column("secret_refs")
        batch_op.drop_column("environment")
