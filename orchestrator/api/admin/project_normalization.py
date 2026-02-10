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

    ask_thread_channel_ids = raw.get("ask_thread_channel_ids")
    if isinstance(ask_thread_channel_ids, list):
        normalized_ask_threads = [str(value).strip() for value in ask_thread_channel_ids if str(value).strip()]
        if normalized_ask_threads:
            normalized["ask_thread_channel_ids"] = normalized_ask_threads

    seed_followup_thread_channel_ids = raw.get("seed_followup_thread_channel_ids")
    if isinstance(seed_followup_thread_channel_ids, list):
        normalized_seed_threads = [
            str(value).strip() for value in seed_followup_thread_channel_ids if str(value).strip()
        ]
        if normalized_seed_threads:
            normalized["seed_followup_thread_channel_ids"] = normalized_seed_threads

    return normalized


def with_preserved_discord_system_fields(*, existing: dict, proposed: dict | None) -> dict | None:
    if proposed is None:
        return None
    merged = dict(proposed)
    for key in DISCORD_INTERNAL_CONFIG_KEYS:
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
    template = str(settings.discord_channel_name_template or "").strip() or "proj-{jira_project_key}-{project_name}"
    includes_project_token = "{project_name}" in template or "{project_id}" in template
    rendered = template.format(
        tenant_id=tenant.tenant_id,
        project_id=project.project_id,
        project_name=project.name,
        jira_project_key=project.jira_project_key,
    )
    if not includes_project_token:
        rendered = f"{rendered}-{project.jira_project_key}-{project.name}"
    sanitized = sanitize_discord_channel_name(rendered)
    if sanitized:
        return sanitized
    fallback = sanitize_discord_channel_name(f"proj-{project.jira_project_key}-{project.name}")
    return fallback or f"proj-{project.jira_project_key}"[:100]
