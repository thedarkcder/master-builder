from __future__ import annotations

import re

from orchestrator.storage.models import Project, Tenant

DISCORD_INTERNAL_CONFIG_KEYS = {
    "pending_ask_actions",
    "ask_history",
    "ask_thread_channel_ids",
    "allowlist_requests",
    "allowed_user_ids",
}
PM_ROOM_LIST_KEYS = (
    "pm_room_channel_ids",
    "pm_room_thread_channel_ids",
    "pm_thread_channel_ids",
)
PM_ROOM_SINGLE_KEYS = (
    "pm_room_channel_id",
    "pm_room_thread_channel_id",
    "pm_thread_channel_id",
)


def default_project_name_from_repo(*, repo_url: str, tenant_id: str) -> str:
    normalized = repo_url.rstrip("/")
    if normalized.endswith(".git"):
        normalized = normalized[:-4]
    name = normalized.rsplit("/", 1)[-1].strip()
    return name or f"{tenant_id}-project"


def normalize_project_repo(repo: str) -> str:
    return repo.strip()


def normalize_project_key(key: str) -> str:
    return key.strip().upper()


def normalize_string_map(raw: dict[str, str] | None) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, str] = {}
    for key, value in raw.items():
        normalized_key = str(key).strip()
        normalized_value = str(value).strip()
        if not normalized_key or not normalized_value:
            continue
        normalized[normalized_key] = normalized_value
    return normalized


def _normalize_channel_id_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def normalize_project_discord_config(raw: dict | None) -> dict:
    if not isinstance(raw, dict):
        return {}

    normalized: dict[str, object] = {}
    channel_id = str(raw.get("channel_id") or "").strip()
    if channel_id:
        normalized["channel_id"] = channel_id

    notify_events = raw.get("notify_events")
    if isinstance(notify_events, list):
        normalized_events = [str(value).strip() for value in notify_events if str(value).strip()]
        if normalized_events:
            normalized["notify_events"] = normalized_events

    normalized_ask_threads = _normalize_channel_id_list(raw.get("ask_thread_channel_ids"))
    if normalized_ask_threads:
        normalized["ask_thread_channel_ids"] = normalized_ask_threads

    normalized_seed_threads = _normalize_channel_id_list(raw.get("seed_followup_thread_channel_ids"))
    if normalized_seed_threads:
        normalized["seed_followup_thread_channel_ids"] = normalized_seed_threads

    for key in PM_ROOM_LIST_KEYS:
        normalized_pm_channels = _normalize_channel_id_list(raw.get(key))
        if normalized_pm_channels:
            normalized[key] = normalized_pm_channels
    for key in PM_ROOM_SINGLE_KEYS:
        normalized_pm_channel = str(raw.get(key) or "").strip()
        if normalized_pm_channel:
            normalized[key] = normalized_pm_channel

    return normalized


def with_preserved_discord_system_fields(*, existing: dict, proposed: dict | None) -> dict | None:
    if proposed is None:
        return None
    merged = dict(proposed)
    preserved_keys = set(DISCORD_INTERNAL_CONFIG_KEYS) | set(PM_ROOM_LIST_KEYS) | set(PM_ROOM_SINGLE_KEYS)
    for key in preserved_keys:
        if key in merged:
            continue
        value = existing.get(key)
        if value is not None:
            merged[key] = value
    return merged


def sanitize_discord_channel_name(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9-]+", "-", value.strip().lower())
    normalized = re.sub(r"-{2,}", "-", normalized).strip("-")
    return normalized[:100]


def resolve_project_discord_channel_name(*, settings, tenant: Tenant, project: Project) -> str:  # noqa: ANN001
    template = str(settings.discord_channel_name_template or "").strip() or "{project_name}"
    includes_project_token = "{project_name}" in template or "{project_id}" in template
    rendered = template.format(
        tenant_id=tenant.tenant_id,
        project_id=project.project_id,
        project_name=project.name,
        jira_project_key=project.jira_project_key,
    )
    if not includes_project_token:
        rendered = f"{rendered}-{project.name}"
    return sanitize_discord_channel_name(rendered)
