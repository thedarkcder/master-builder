"""remove legacy live voice pair fields from discord config

Revision ID: 20260322_0034
Revises: 20260322_0033
Create Date: 2026-03-22 22:15:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260322_0034"
down_revision = "20260322_0033"
branch_labels = None
depends_on = None


def _normalize_channel_id_map(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, str] = {}
    for key, raw_value in value.items():
        normalized_key = str(key or "").strip()
        normalized_value = str(raw_value or "").strip()
        if not normalized_key or not normalized_value:
            continue
        normalized[normalized_key] = normalized_value
    return normalized


def _rewrite_discord_config(raw_value: object) -> dict | None:
    if raw_value is None:
        return None
    if not isinstance(raw_value, dict):
        return dict(raw_value or {})
    discord_config = dict(raw_value)
    normalized_links = _normalize_channel_id_map(discord_config.get("live_voice_room_links"))
    if not normalized_links:
        legacy_voice_channel_id = str(discord_config.get("live_voice_channel_id") or "").strip()
        legacy_linked_channel_id = str(discord_config.get("live_voice_linked_text_channel_id") or "").strip()
        if legacy_voice_channel_id and legacy_linked_channel_id:
            normalized_links = {
                legacy_voice_channel_id: legacy_linked_channel_id,
            }

    discord_config.pop("live_voice_channel_id", None)
    discord_config.pop("live_voice_linked_text_channel_id", None)
    if normalized_links:
        discord_config["live_voice_room_links"] = normalized_links
    else:
        discord_config.pop("live_voice_room_links", None)
    return discord_config


def upgrade() -> None:
    bind = op.get_bind()
    tenants = sa.table(
        "tenants",
        sa.column("tenant_id", sa.String()),
        sa.column("discord_config", sa.JSON()),
    )
    projects = sa.table(
        "projects",
        sa.column("project_id", sa.String()),
        sa.column("discord_config", sa.JSON()),
    )

    tenant_rows = bind.execute(sa.select(tenants.c.tenant_id, tenants.c.discord_config)).all()
    for row in tenant_rows:
        current_config = row.discord_config
        rewritten_config = _rewrite_discord_config(current_config)
        if rewritten_config != current_config:
            bind.execute(
                tenants.update()
                .where(tenants.c.tenant_id == str(row.tenant_id))
                .values(discord_config=rewritten_config)
            )

    project_rows = bind.execute(sa.select(projects.c.project_id, projects.c.discord_config)).all()
    for row in project_rows:
        current_config = row.discord_config
        rewritten_config = _rewrite_discord_config(current_config)
        if rewritten_config != current_config:
            bind.execute(
                projects.update()
                .where(projects.c.project_id == str(row.project_id))
                .values(discord_config=rewritten_config)
            )


def downgrade() -> None:
    # Migration is intentionally non-destructive.
    pass
