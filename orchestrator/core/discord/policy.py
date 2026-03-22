from __future__ import annotations

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


def normalize_allowlisted_user_ids(discord_config: dict | None) -> set[str]:
    discord_config = discord_config or {}
    raw_allowlist = discord_config.get("allowed_user_ids")
    if not isinstance(raw_allowlist, list):
        return set()
    return {str(user_id).strip() for user_id in raw_allowlist if str(user_id).strip()}


def normalize_allowlist_requests(discord_config: dict | None) -> list[dict]:
    discord_config = discord_config or {}
    raw_requests = discord_config.get("allowlist_requests")
    if not isinstance(raw_requests, list):
        return []
    normalized: list[dict] = []
    for item in raw_requests:
        if not isinstance(item, dict):
            continue
        user_id = str(item.get("user_id") or "").strip()
        if not user_id:
            continue
        normalized.append(
            {
                "user_id": user_id,
                "requested_at": str(item.get("requested_at") or "").strip(),
                "channel_id": str(item.get("channel_id") or "").strip() or None,
                "reason": str(item.get("reason") or "").strip() or None,
                "permissions": [str(value).strip() for value in (item.get("permissions") or []) if str(value).strip()],
            }
        )
    return normalized


def channel_ids_from_discord_config(discord_config: dict | None) -> set[str]:
    discord_config = discord_config or {}
    allowed: set[str] = set()
    configured_channel_id = str(discord_config.get("channel_id") or "").strip()
    if configured_channel_id:
        allowed.add(configured_channel_id)

    raw_thread_ids = discord_config.get("ask_thread_channel_ids")
    if isinstance(raw_thread_ids, list):
        for value in raw_thread_ids:
            normalized = str(value or "").strip()
            if normalized:
                allowed.add(normalized)
    raw_seed_thread_ids = discord_config.get("seed_followup_thread_channel_ids")
    if isinstance(raw_seed_thread_ids, list):
        for value in raw_seed_thread_ids:
            normalized = str(value or "").strip()
            if normalized:
                allowed.add(normalized)

    for key in ROOM_LIST_KEYS:
        raw_values = discord_config.get(key)
        if not isinstance(raw_values, list):
            continue
        for value in raw_values:
            normalized = str(value or "").strip()
            if normalized:
                allowed.add(normalized)
    for key in ROOM_SINGLE_KEYS:
        normalized = str(discord_config.get(key) or "").strip()
        if normalized:
            allowed.add(normalized)

    raw_live_voice_room_links = discord_config.get("live_voice_room_links")
    if isinstance(raw_live_voice_room_links, dict):
        for voice_channel_id, linked_text_channel_id in raw_live_voice_room_links.items():
            normalized_voice_channel_id = str(voice_channel_id or "").strip()
            normalized_linked_text_channel_id = str(linked_text_channel_id or "").strip()
            if normalized_voice_channel_id:
                allowed.add(normalized_voice_channel_id)
            if normalized_linked_text_channel_id:
                allowed.add(normalized_linked_text_channel_id)
    return allowed


def can_execute_sensitive_command(
    *,
    command_name: str,
    user_id: str,
    has_project_mapping: bool,
    tenant_allowlist: set[str],
    project_allowlist: set[str],
) -> tuple[bool, str | None]:
    if user_id in tenant_allowlist:
        return True, None
    if not has_project_mapping:
        return False, f"'{command_name}' requires a project-mapped Discord channel"
    if user_id in project_allowlist:
        return True, None
    return False, f"'{command_name}' requires an allowlisted Discord user for this project"


def is_channel_allowed(*, channel_id: str | None, allowed_channel_ids: set[str]) -> bool:
    if not channel_id:
        return True
    if not allowed_channel_ids:
        return True
    return channel_id in allowed_channel_ids
