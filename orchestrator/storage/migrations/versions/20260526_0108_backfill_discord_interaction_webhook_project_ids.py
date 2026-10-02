"""Backfill Discord interaction webhook project scope.

Revision ID: 20260526_0108
Revises: 20260523_0107
Create Date: 2026-05-26 10:30:00.000000
"""

from __future__ import annotations

import json
from collections import defaultdict

from alembic import op
import sqlalchemy as sa


revision = "20260526_0108"
down_revision = "20260523_0107"
branch_labels = None
depends_on = None

ROOM_LIST_KEYS = (
    "voice_room_channel_ids",
    "voice_room_thread_channel_ids",
    "voice_thread_channel_ids",
    "persona_room_channel_ids",
    "persona_room_thread_channel_ids",
    "persona_thread_channel_ids",
    "room_channel_ids",
    "room_thread_channel_ids",
    "pm_room_channel_ids",
    "pm_room_thread_channel_ids",
    "pm_thread_channel_ids",
)
ROOM_SINGLE_KEYS = (
    "voice_room_channel_id",
    "voice_room_thread_channel_id",
    "voice_thread_channel_id",
    "persona_room_channel_id",
    "persona_room_thread_channel_id",
    "persona_thread_channel_id",
    "room_channel_id",
    "room_thread_channel_id",
    "pm_room_channel_id",
    "pm_room_thread_channel_id",
    "pm_thread_channel_id",
)


def _parse_json(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _channel_ids_from_discord_config(discord_config: dict[str, object]) -> set[str]:
    allowed: set[str] = set()
    configured_channel_id = str(discord_config.get("channel_id") or "").strip()
    if configured_channel_id:
        allowed.add(configured_channel_id)
    for key in (
        "ask_thread_channel_ids",
        "seed_followup_thread_channel_ids",
        *ROOM_LIST_KEYS,
    ):
        raw_values = discord_config.get(key)
        if not isinstance(raw_values, list):
            continue
        allowed.update(
            str(value or "").strip() for value in raw_values if str(value or "").strip()
        )
    for key in ROOM_SINGLE_KEYS:
        normalized = str(discord_config.get(key) or "").strip()
        if normalized:
            allowed.add(normalized)
    raw_live_voice_room_links = discord_config.get("live_voice_room_links")
    if isinstance(raw_live_voice_room_links, dict):
        for (
            voice_channel_id,
            linked_text_channel_id,
        ) in raw_live_voice_room_links.items():
            normalized_voice_channel_id = str(voice_channel_id or "").strip()
            normalized_linked_text_channel_id = str(
                linked_text_channel_id or ""
            ).strip()
            if normalized_voice_channel_id:
                allowed.add(normalized_voice_channel_id)
            if normalized_linked_text_channel_id:
                allowed.add(normalized_linked_text_channel_id)
    return allowed


def _payload_channel_id(payload_json: object) -> str | None:
    payload = _parse_json(payload_json)
    channel_id = str(payload.get("channel_id") or "").strip()
    return channel_id or None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not {"projects", "tenants", "webhook_jobs"}.issubset(
        set(inspector.get_table_names())
    ):
        return

    project_rows = bind.execute(
        sa.text(
            """
            SELECT
                project.project_id,
                project.tenant_id,
                project.discord_config,
                tenant.discord_config AS tenant_discord_config
            FROM projects project
            JOIN tenants tenant ON tenant.tenant_id = project.tenant_id
            WHERE project.is_archived = FALSE
            """
        )
    ).mappings()

    tenant_projects: dict[str, list[str]] = defaultdict(list)
    channel_project_candidates: dict[tuple[str, str], list[str]] = defaultdict(list)
    tenant_primary_channels: dict[str, str] = {}
    for row in project_rows:
        tenant_id = str(row["tenant_id"] or "").strip()
        project_id = str(row["project_id"] or "").strip()
        if not tenant_id or not project_id:
            continue
        tenant_projects[tenant_id].append(project_id)
        for channel_id in _channel_ids_from_discord_config(
            _parse_json(row["discord_config"])
        ):
            channel_project_candidates[(tenant_id, channel_id)].append(project_id)
        tenant_discord_config = _parse_json(row["tenant_discord_config"])
        tenant_channel_id = str(tenant_discord_config.get("channel_id") or "").strip()
        if tenant_channel_id:
            tenant_primary_channels[tenant_id] = tenant_channel_id

    for tenant_id, channel_id in tenant_primary_channels.items():
        project_ids = tenant_projects.get(tenant_id, [])
        if len(project_ids) == 1:
            channel_project_candidates[(tenant_id, channel_id)].append(project_ids[0])

    unique_channel_project = {
        key: candidates[0]
        for key, candidates in channel_project_candidates.items()
        if len(set(candidates)) == 1
    }
    if not unique_channel_project:
        return

    job_rows = bind.execute(
        sa.text(
            """
            SELECT job_id, tenant_id, payload_json
            FROM webhook_jobs
            WHERE transport = 'discord_interaction'
              AND project_id IS NULL
            """
        )
    ).mappings()
    for row in job_rows:
        tenant_id = str(row["tenant_id"] or "").strip()
        channel_id = _payload_channel_id(row["payload_json"])
        if not tenant_id or not channel_id:
            continue
        project_id = unique_channel_project.get((tenant_id, channel_id))
        if not project_id:
            continue
        bind.execute(
            sa.text(
                """
                UPDATE webhook_jobs
                SET project_id = :project_id,
                    updated_at = CURRENT_TIMESTAMP
                WHERE job_id = :job_id
                """
            ),
            {"project_id": project_id, "job_id": row["job_id"]},
        )


def downgrade() -> None:
    raise RuntimeError(
        "Discord interaction webhook project scope backfill cannot be downgraded"
    )
