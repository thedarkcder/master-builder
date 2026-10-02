from __future__ import annotations

import re

THREAD_ISSUE_BY_CHANNEL_ID_KEY = "thread_issue_by_channel_id"
_ISSUE_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")


def normalize_issue_key(value: str | None) -> str:
    normalized = str(value or "").strip().upper()
    if _ISSUE_KEY_PATTERN.fullmatch(normalized) is None:
        return ""
    return normalized


def get_thread_issue_key(*, discord_config: dict | None, channel_id: str) -> str:
    normalized_channel_id = str(channel_id or "").strip()
    if not normalized_channel_id:
        return ""
    config = dict(discord_config or {})
    raw_map = config.get(THREAD_ISSUE_BY_CHANNEL_ID_KEY)
    issue_map = raw_map if isinstance(raw_map, dict) else {}
    issue_key = normalize_issue_key(issue_map.get(normalized_channel_id))
    if issue_key:
        return issue_key
    return ""


def put_thread_issue_key(
    *,
    discord_config: dict | None,
    channel_id: str,
    issue_key: str,
    max_entries: int = 500,
) -> dict:
    updated = dict(discord_config or {})
    normalized_channel_id = str(channel_id or "").strip()
    normalized_issue_key = normalize_issue_key(issue_key)
    if not normalized_channel_id or not normalized_issue_key:
        return updated
    raw_map = updated.get(THREAD_ISSUE_BY_CHANNEL_ID_KEY)
    issue_map = dict(raw_map) if isinstance(raw_map, dict) else {}
    issue_map[normalized_channel_id] = normalized_issue_key
    updated[THREAD_ISSUE_BY_CHANNEL_ID_KEY] = dict(
        list(issue_map.items())[-max_entries:]
    )
    return updated


def remove_thread_issue_key(*, discord_config: dict | None, channel_id: str) -> dict:
    updated = dict(discord_config or {})
    normalized_channel_id = str(channel_id or "").strip()
    if not normalized_channel_id:
        return updated
    raw_map = updated.get(THREAD_ISSUE_BY_CHANNEL_ID_KEY)
    issue_map = dict(raw_map) if isinstance(raw_map, dict) else {}
    if normalized_channel_id in issue_map:
        issue_map.pop(normalized_channel_id, None)
    updated[THREAD_ISSUE_BY_CHANNEL_ID_KEY] = issue_map
    return updated
