from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from orchestrator.api.discord.state_repository import (
    resolve_project_for_discord_channel,
    save_project_allowlist_requests,
    save_seed_followups,
    tenant_allowed_channel_ids,
)
from orchestrator.core.discord.policy import (
    can_execute_sensitive_command,
    is_channel_allowed,
    normalize_allowlist_requests,
    normalize_allowlisted_user_ids,
)
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
    return normalize_allowlisted_user_ids(tenant.discord_config or {})


def project_allowlisted_user_ids(project: Project) -> set[str]:
    return normalize_allowlisted_user_ids(project.discord_config or {})


def tenant_allowlist_requests(tenant: Tenant) -> list[dict]:
    normalized = normalize_allowlist_requests(tenant.discord_config or {})
    for item in normalized:
        if not item.get("requested_at"):
            item["requested_at"] = datetime.now(timezone.utc).isoformat()
    return normalized


def project_allowlist_requests(project: Project) -> list[dict]:
    normalized = normalize_allowlist_requests(project.discord_config or {})
    for item in normalized:
        if not item.get("requested_at"):
            item["requested_at"] = datetime.now(timezone.utc).isoformat()
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
    legacy_tenant_allowlist = tenant_allowlisted_user_ids(tenant)
    if user_id in legacy_tenant_allowlist:
        return False, "You are already allowlisted for sensitive commands in this project."

    project = resolve_project_for_discord_channel(
        session=session,
        tenant_id=tenant.tenant_id,
        channel_id=channel_id,
    )
    if project is None:
        return False, "Allowlist requests must be sent from a mapped project Discord channel."

    allowlisted_ids = project_allowlisted_user_ids(project)
    if user_id in allowlisted_ids:
        return False, "You are already allowlisted for sensitive commands in this project."

    requests = project_allowlist_requests(project)
    existing = next((entry for entry in requests if entry.get("user_id") == user_id), None)
    now_iso = datetime.now(timezone.utc).isoformat()
    if existing:
        existing["requested_at"] = now_iso
        existing["channel_id"] = channel_id
        existing["permissions"] = permissions
        existing["reason"] = reason
        message = "Allowlist request refreshed. An admin can approve it in the project Discord page."
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
        message = "Allowlist request submitted. An admin can approve it in the project Discord page."

    save_project_allowlist_requests(
        session=session,
        tenant=tenant,
        project=project,
        requests=requests,
    )
    return True, message


def assert_sensitive_command_permission(
    *,
    session: Session,
    tenant: Tenant,
    command_name: str,
    user_id: str,
    channel_id: str | None,
) -> None:
    if command_name not in SENSITIVE_COMMANDS:
        return
    project = resolve_project_for_discord_channel(
        session=session,
        tenant_id=tenant.tenant_id,
        channel_id=channel_id,
    )
    legacy_tenant_allowlist = tenant_allowlisted_user_ids(tenant)
    project_allowlist = project_allowlisted_user_ids(project) if project is not None else set()
    permitted, reason = can_execute_sensitive_command(
        command_name=command_name,
        user_id=user_id,
        has_project_mapping=project is not None,
        tenant_allowlist=legacy_tenant_allowlist,
        project_allowlist=project_allowlist,
    )
    if permitted:
        return
    if reason is None:
        reason = f"'{command_name}' requires an allowlisted Discord user for this project"
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=reason)


def assert_channel_scope(*, session: Session, tenant: Tenant, channel_id: str | None) -> None:
    allowed_channel_ids = tenant_allowed_channel_ids(session=session, tenant=tenant)
    if not is_channel_allowed(channel_id=channel_id, allowed_channel_ids=allowed_channel_ids):
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

    removed_channel_ids: set[str] = set()
    if isinstance(removed_entry, dict):
        removed_channels = removed_entry.get("channel_ids")
        if isinstance(removed_channels, list):
            removed_channel_ids = {str(value).strip() for value in removed_channels if str(value).strip()}
    save_seed_followups(
        session=session,
        tenant=tenant,
        entries=kept_entries,
        removed_channel_ids=removed_channel_ids,
    )
