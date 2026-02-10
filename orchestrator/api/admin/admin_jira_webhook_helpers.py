from __future__ import annotations

import re
from urllib.parse import quote


def default_ready_jql(*, project_keys: list[str], ready_statuses: list[str]) -> str:
    quoted_projects = ", ".join(f'"{key}"' for key in project_keys)
    quoted_statuses = ", ".join(f'"{status}"' for status in ready_statuses)
    return f"project in ({quoted_projects}) AND status in ({quoted_statuses}) ORDER BY updated DESC"


def parse_managed_webhook_ids(jira_config: dict) -> list[int]:
    raw_ids = jira_config.get("managed_webhook_ids")
    if not isinstance(raw_ids, list):
        return []
    parsed: list[int] = []
    for value in raw_ids:
        webhook_id = parse_jira_webhook_id(value)
        if webhook_id is not None:
            parsed.append(webhook_id)
    return parsed


def parse_jira_webhook_id(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        if stripped.isdigit():
            return int(stripped)
    return None


def is_jira_webhook_limit_error(exc: Exception) -> bool:
    lowered = str(exc).lower()
    return (
        "maximum of 5 webhooks is allowed per app per user" in lowered
        or "maximum number of webhooks already registered" in lowered
    )


def is_jira_webhook_single_url_error(exc: Exception) -> bool:
    return "only a single url per user is allowed to be registered via rest api" in str(exc).lower()


def extract_jira_webhook_conflict_url(exc: Exception) -> str | None:
    match = re.search(r"currently used url:\s*(https?://\S+)", str(exc), flags=re.IGNORECASE)
    if match is None:
        return None
    return match.group(1).rstrip(").,; ")


def jira_webhook_callback_url(*, settings, tenant_id: str) -> str:  # noqa: ANN001
    return f"{settings.public_api_base_url.rstrip('/')}/jira/webhook/{quote(tenant_id, safe='')}"


def jira_webhook_filter_jql(jira_config: dict) -> str:
    project_keys = jira_config.get("project_keys")
    if not isinstance(project_keys, list) or not project_keys:
        raise ValueError("Missing Jira project_keys")
    quoted_projects = ", ".join(f"\"{str(key).strip()}\"" for key in project_keys if str(key).strip())
    if not quoted_projects:
        raise ValueError("Missing Jira project_keys")
    return f"project in ({quoted_projects}) ORDER BY updated DESC"
