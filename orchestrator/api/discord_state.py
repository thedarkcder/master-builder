from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.storage.models import Project, Tenant

SENSITIVE_COMMANDS = {"run", "cancel", "retry", "promote", "issues"}
PUBLIC_COMMANDS = {"help", "status", "runs", "policy", "link", "ask", "gap", "request", "bug"}
SUPPORTED_COMMANDS = SENSITIVE_COMMANDS | PUBLIC_COMMANDS
REQUEST_PERMISSION_LABELS = {
    "run_controls": "run controls (!run, !cancel, !retry)",
    "seed_issues": "issue seeding (!issues seed)",
    "all_sensitive": "all sensitive commands",
}
MAX_PENDING_SEED_FOLLOWUPS = 30


def normalize_status_name(value: str) -> str:
    return value.strip().lower()


def parse_command_text(command_text: str) -> tuple[str, list[str]]:
    normalized = command_text.strip()
    if not normalized.startswith("!"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Commands must start with '!'")

    parts = [part for part in normalized[1:].split(" ") if part]
    if not parts:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing command name")

    command_name = parts[0].strip().lower()
    arguments = parts[1:]
    if command_name not in SUPPORTED_COMMANDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported command '{command_name}'",
        )
    return command_name, arguments


def tenant_allowlisted_user_ids(tenant: Tenant) -> set[str]:
    discord_config = tenant.discord_config or {}
    raw_allowlist = discord_config.get("allowed_user_ids")
    if not isinstance(raw_allowlist, list):
        return set()
    normalized = {str(user_id).strip() for user_id in raw_allowlist if str(user_id).strip()}
    return normalized


def tenant_allowlist_requests(tenant: Tenant) -> list[dict]:
    discord_config = tenant.discord_config or {}
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
                "requested_at": str(item.get("requested_at") or "").strip() or datetime.now(timezone.utc).isoformat(),
                "channel_id": str(item.get("channel_id") or "").strip() or None,
                "reason": str(item.get("reason") or "").strip() or None,
            }
        )
    return normalized


def create_allowlist_request(
    *,
    session: Session,
    tenant: Tenant,
    user_id: str,
    channel_id: str | None,
    permissions: list[str],
    reason: str | None,
) -> tuple[bool, str]:
    allowlisted_ids = tenant_allowlisted_user_ids(tenant)
    if user_id in allowlisted_ids:
        return False, "You are already allowlisted for sensitive commands."

    requests = tenant_allowlist_requests(tenant)
    existing = next((entry for entry in requests if entry.get("user_id") == user_id), None)
    now_iso = datetime.now(timezone.utc).isoformat()
    if existing:
        existing["requested_at"] = now_iso
        existing["channel_id"] = channel_id
        existing["permissions"] = permissions
        existing["reason"] = reason
        message = "Allowlist request refreshed. An admin can approve it in the tenant page."
    else:
        requests.append(
            {
                "user_id": user_id,
                "requested_at": now_iso,
                "channel_id": channel_id,
                "permissions": permissions,
                "reason": reason,
            }
        )
        message = "Allowlist request submitted. An admin can approve it in the tenant page."

    discord_config = dict(tenant.discord_config or {})
    discord_config["allowlist_requests"] = requests
    tenant.discord_config = discord_config
    tenant.updated_at = datetime.now(timezone.utc)
    session.commit()
    return True, message


def assert_sensitive_command_permission(*, tenant: Tenant, command_name: str, user_id: str) -> None:
    if command_name not in SENSITIVE_COMMANDS:
        return
    allowlist = tenant_allowlisted_user_ids(tenant)
    if user_id not in allowlist:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"'{command_name}' requires an allowlisted Discord user",
        )


def _channel_ids_from_discord_config(discord_config: dict | None) -> set[str]:
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
    return allowed


def project_allowed_channel_ids(*, session: Session, tenant_id: str) -> set[str]:
    projects = session.execute(
        select(Project).where(
            Project.tenant_id == tenant_id,
            Project.is_archived.is_(False),
        )
    ).scalars().all()
    allowed: set[str] = set()
    for project in projects:
        allowed.update(_channel_ids_from_discord_config(dict(project.discord_config or {})))
    return allowed


def tenant_allowed_channel_ids(*, session: Session, tenant: Tenant) -> set[str]:
    # Project-level channel bindings take precedence, with tenant-level fallback for backward compatibility.
    allowed = project_allowed_channel_ids(session=session, tenant_id=tenant.tenant_id)
    allowed.update(_channel_ids_from_discord_config(dict(tenant.discord_config or {})))
    return allowed


def assert_channel_scope(*, session: Session, tenant: Tenant, channel_id: str | None) -> None:
    if not channel_id:
        return
    allowed_channel_ids = tenant_allowed_channel_ids(session=session, tenant=tenant)
    if allowed_channel_ids and channel_id not in allowed_channel_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Command channel does not match project or tenant Discord channel",
        )


def tenant_seed_followups(tenant: Tenant) -> list[dict]:
    discord_config = tenant.discord_config or {}
    raw_entries = discord_config.get("seed_followups")
    if not isinstance(raw_entries, list):
        return []
    normalized: list[dict] = []
    for item in raw_entries:
        if not isinstance(item, dict):
            continue
        request_id = str(item.get("request_id") or "").strip()
        if not request_id:
            continue
        channel_ids_raw = item.get("channel_ids")
        channel_ids = (
            [str(value).strip() for value in channel_ids_raw if str(value).strip()]
            if isinstance(channel_ids_raw, list)
            else []
        )
        questions_raw = item.get("questions")
        questions = (
            [str(value).strip() for value in questions_raw if str(value).strip()]
            if isinstance(questions_raw, list)
            else []
        )
        issue_keys_raw = item.get("issue_keys")
        issue_keys = (
            [str(value).strip().upper() for value in issue_keys_raw if str(value).strip()]
            if isinstance(issue_keys_raw, list)
            else []
        )
        prompt_markdown = str(item.get("prompt_markdown") or "").strip()
        if not prompt_markdown:
            continue
        normalized.append(
            {
                "request_id": request_id,
                "user_id": str(item.get("user_id") or "").strip() or None,
                "channel_ids": channel_ids,
                "questions": questions,
                "issue_keys": issue_keys,
                "project_key": str(item.get("project_key") or "").strip().upper() or None,
                "prompt_markdown": prompt_markdown,
                "updated_at": str(item.get("updated_at") or "").strip() or datetime.now(timezone.utc).isoformat(),
            }
        )
    return normalized


def store_seed_followup_context(
    *,
    session: Session,
    tenant: Tenant,
    request_id: str | None,
    user_id: str,
    channel_ids: list[str],
    project_key: str,
    issue_keys: list[str],
    questions: list[str],
    prompt_markdown: str,
) -> str:
    normalized_channel_ids = [value.strip() for value in channel_ids if value and value.strip()]
    normalized_issue_keys = [value.strip().upper() for value in issue_keys if value and value.strip()]
    normalized_questions = [value.strip() for value in questions if value and value.strip()]
    normalized_request_id = (request_id or "").strip() or uuid4().hex

    now_iso = datetime.now(timezone.utc).isoformat()
    entries = tenant_seed_followups(tenant)
    updated_entries: list[dict] = []
    stored = False
    for entry in entries:
        if entry.get("request_id") != normalized_request_id:
            updated_entries.append(entry)
            continue
        updated_entries.append(
            {
                "request_id": normalized_request_id,
                "user_id": user_id.strip() or entry.get("user_id"),
                "channel_ids": normalized_channel_ids or entry.get("channel_ids", []),
                "questions": normalized_questions or entry.get("questions", []),
                "issue_keys": normalized_issue_keys or entry.get("issue_keys", []),
                "project_key": project_key.strip().upper() or entry.get("project_key"),
                "prompt_markdown": prompt_markdown.strip() or entry.get("prompt_markdown", ""),
                "updated_at": now_iso,
            }
        )
        stored = True
    if not stored:
        updated_entries.append(
            {
                "request_id": normalized_request_id,
                "user_id": user_id.strip() or None,
                "channel_ids": normalized_channel_ids,
                "questions": normalized_questions,
                "issue_keys": normalized_issue_keys,
                "project_key": project_key.strip().upper() or None,
                "prompt_markdown": prompt_markdown.strip(),
                "updated_at": now_iso,
            }
        )

    discord_config = dict(tenant.discord_config or {})
    discord_config["seed_followups"] = updated_entries[-MAX_PENDING_SEED_FOLLOWUPS:]
    tenant.discord_config = discord_config
    tenant.updated_at = datetime.now(timezone.utc)
    session.commit()
    return normalized_request_id


def find_seed_followup_context(
    *,
    tenant: Tenant,
    channel_id: str,
) -> dict | None:
    normalized_channel_id = channel_id.strip()
    if not normalized_channel_id:
        return None
    entries = tenant_seed_followups(tenant)
    for entry in reversed(entries):
        channel_ids = entry.get("channel_ids")
        if not isinstance(channel_ids, list):
            continue
        if normalized_channel_id in channel_ids:
            return entry
    return None


def clear_seed_followup_context(
    *,
    session: Session,
    tenant: Tenant,
    request_id: str,
) -> None:
    normalized_request_id = request_id.strip()
    if not normalized_request_id:
        return
    entries = tenant_seed_followups(tenant)
    kept_entries = [entry for entry in entries if entry.get("request_id") != normalized_request_id]
    removed_entry = next((entry for entry in entries if entry.get("request_id") == normalized_request_id), None)

    discord_config = dict(tenant.discord_config or {})
    discord_config["seed_followups"] = kept_entries
    if isinstance(removed_entry, dict):
        raw_seed_thread_ids = discord_config.get("seed_followup_thread_channel_ids")
        seed_thread_ids = (
            [str(value).strip() for value in raw_seed_thread_ids if str(value).strip()]
            if isinstance(raw_seed_thread_ids, list)
            else []
        )
        removed_channels = removed_entry.get("channel_ids")
        if isinstance(removed_channels, list):
            removed_set = {str(value).strip() for value in removed_channels if str(value).strip()}
            if removed_set:
                seed_thread_ids = [value for value in seed_thread_ids if value not in removed_set]
        discord_config["seed_followup_thread_channel_ids"] = seed_thread_ids
    tenant.discord_config = discord_config
    tenant.updated_at = datetime.now(timezone.utc)
    session.commit()
