from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.api.discord.shared.state_repository import (
    resolve_project_for_discord_channel,
    save_project_allowlist_requests,
    tenant_allowed_channel_ids,
)
from orchestrator.core.clarification.questions import ClarificationQuestionSet
from orchestrator.core.pm.followup_context_service import (
    FOLLOWUP_CONTEXT_SEED_FOLLOWUP,
    close_followup_contexts,
    upsert_followup_context,
)
from orchestrator.core.discord.policy import (
    can_execute_sensitive_command,
    is_channel_allowed,
    normalize_allowlist_requests,
    normalize_allowlisted_user_ids,
)
from orchestrator.storage.models import FollowupContext, Project, Tenant

SENSITIVE_COMMANDS = {"run", "cancel", "retry", "reply", "issues"}
PUBLIC_COMMANDS = {
    "help",
    "status",
    "runs",
    "policy",
    "link",
    "ask",
    "pm",
    "gap",
    "request",
    "bug",
    "architect",
    "engineer",
    "tester",
    "security",
    "reviewer",
}
SUPPORTED_COMMANDS = SENSITIVE_COMMANDS | PUBLIC_COMMANDS
REQUEST_PERMISSION_LABELS = {
    "run_controls": "run controls (!run, !cancel, !retry)",
    "seed_issues": "PM batch seeding (!issues seed)",
    "all_sensitive": "all sensitive commands",
}
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
LIVE_VOICE_LINK_KEYS = ("live_voice_room_links",)
MAX_PENDING_SEED_FOLLOWUP_AGE = timedelta(hours=24)


def normalize_status_name(value: str) -> str:
    return value.strip().lower()


def _room_channel_ids_from_discord_config(discord_config: dict | None) -> set[str]:
    config = dict(discord_config or {})
    channel_ids: set[str] = set()
    for key in ROOM_LIST_KEYS:
        raw_values = config.get(key)
        if not isinstance(raw_values, list):
            continue
        for value in raw_values:
            normalized = str(value or "").strip()
            if normalized:
                channel_ids.add(normalized)
    for key in ROOM_SINGLE_KEYS:
        normalized = str(config.get(key) or "").strip()
        if normalized:
            channel_ids.add(normalized)
    return channel_ids


def room_channel_ids_from_discord_config(discord_config: dict | None) -> set[str]:
    return _room_channel_ids_from_discord_config(discord_config)


def live_voice_enabled_from_discord_config(discord_config: dict | None) -> bool:
    value = (discord_config or {}).get("live_voice_enabled")
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    return False


def _live_voice_room_links_from_discord_config(
    discord_config: dict | None,
) -> dict[str, str]:
    config = dict(discord_config or {})
    room_links: dict[str, str] = {}
    for key in LIVE_VOICE_LINK_KEYS:
        raw_value = config.get(key)
        if not isinstance(raw_value, dict):
            continue
        for voice_channel_id, linked_channel_id in raw_value.items():
            normalized_voice_channel_id = str(voice_channel_id or "").strip()
            normalized_linked_channel_id = str(linked_channel_id or "").strip()
            if not normalized_voice_channel_id or not normalized_linked_channel_id:
                continue
            room_links[normalized_voice_channel_id] = normalized_linked_channel_id
    return room_links


def live_voice_room_links_from_discord_config(
    discord_config: dict | None,
) -> dict[str, str]:
    return _live_voice_room_links_from_discord_config(discord_config)


def live_voice_room_channel_ids_from_discord_config(
    discord_config: dict | None,
) -> set[str]:
    return set(_live_voice_room_links_from_discord_config(discord_config))


def live_voice_linked_channel_ids_from_discord_config(
    discord_config: dict | None,
) -> set[str]:
    return set(_live_voice_room_links_from_discord_config(discord_config).values())


def project_room_channel_ids(*, session: Session, tenant_id: str) -> set[str]:
    projects = (
        session.execute(
            select(Project).where(
                Project.tenant_id == tenant_id,
                Project.is_archived.is_(False),
            )
        )
        .scalars()
        .all()
    )
    channel_ids: set[str] = set()
    for project in projects:
        channel_ids.update(
            _room_channel_ids_from_discord_config(project.discord_config or {})
        )
    return channel_ids


def project_pm_room_channel_ids(*, session: Session, tenant_id: str) -> set[str]:
    return project_room_channel_ids(session=session, tenant_id=tenant_id)


def _pm_room_channel_ids_from_discord_config(discord_config: dict | None) -> set[str]:
    return _room_channel_ids_from_discord_config(discord_config)


def project_live_voice_room_channel_ids(
    *, session: Session, tenant_id: str
) -> set[str]:
    projects = (
        session.execute(
            select(Project).where(
                Project.tenant_id == tenant_id,
                Project.is_archived.is_(False),
            )
        )
        .scalars()
        .all()
    )
    channel_ids: set[str] = set()
    for project in projects:
        channel_ids.update(
            live_voice_room_channel_ids_from_discord_config(
                project.discord_config or {}
            )
        )
    return channel_ids


def project_live_voice_room_links(
    *, session: Session, tenant_id: str
) -> dict[str, str]:
    projects = (
        session.execute(
            select(Project).where(
                Project.tenant_id == tenant_id,
                Project.is_archived.is_(False),
            )
        )
        .scalars()
        .all()
    )
    room_links: dict[str, str] = {}
    for project in projects:
        room_links.update(
            live_voice_room_links_from_discord_config(project.discord_config or {})
        )
    return room_links


def project_live_voice_linked_channel_ids(
    *, session: Session, tenant_id: str
) -> set[str]:
    return set(
        project_live_voice_room_links(session=session, tenant_id=tenant_id).values()
    )


def parse_command_text(command_text: str) -> tuple[str, list[str]]:
    normalized = command_text.strip()
    if not normalized.startswith("!"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Commands must start with '!'",
        )

    parts = [part for part in normalized[1:].split(" ") if part]
    if not parts:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Missing command name"
        )

    command_name = parts[0].strip().lower()
    arguments = parts[1:]
    if command_name not in SUPPORTED_COMMANDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported command '{command_name}'",
        )
    return command_name, arguments


def command_matches(
    command_text: str, *, command_name: str, subcommand: str | None = None
) -> bool:
    try:
        parsed_command_name, arguments = parse_command_text(command_text)
    except HTTPException:
        return False
    if parsed_command_name != command_name.strip().lower():
        return False
    if subcommand is None:
        return True
    if not arguments:
        return False
    return arguments[0].strip().lower() == subcommand.strip().lower()


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
    project = resolve_project_for_discord_channel(
        session=session,
        tenant_id=tenant.tenant_id,
        channel_id=channel_id,
    )
    if project is None:
        return (
            False,
            "Allowlist requests must be sent from a mapped project Discord channel.",
        )

    allowlisted_ids = project_allowlisted_user_ids(project)
    if user_id in allowlisted_ids:
        return (
            False,
            "You are already allowlisted for sensitive commands in this project.",
        )

    requests = project_allowlist_requests(project)
    existing = next(
        (entry for entry in requests if entry.get("user_id") == user_id), None
    )
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
    project_allowlist = (
        project_allowlisted_user_ids(project) if project is not None else set()
    )
    tenant_allowlist = normalize_allowlisted_user_ids(tenant.discord_config or {})
    permitted, reason = can_execute_sensitive_command(
        command_name=command_name,
        user_id=user_id,
        has_project_mapping=project is not None,
        tenant_allowlist=tenant_allowlist,
        project_allowlist=project_allowlist,
    )
    if permitted:
        return
    if reason is None:
        reason = (
            f"'{command_name}' requires an allowlisted Discord user for this project"
        )
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=reason)


def assert_channel_scope(
    *, session: Session, tenant: Tenant, channel_id: str | None
) -> None:
    allowed_channel_ids = tenant_allowed_channel_ids(session=session, tenant=tenant)
    allowed_channel_ids.update(
        project_room_channel_ids(session=session, tenant_id=tenant.tenant_id)
    )
    allowed_channel_ids.update(
        _room_channel_ids_from_discord_config(tenant.discord_config or {})
    )
    if not is_channel_allowed(
        channel_id=channel_id, allowed_channel_ids=allowed_channel_ids
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Command channel does not match project or tenant Discord channel",
        )


def _parse_iso_timestamp(raw_value: str) -> datetime | None:
    normalized = str(raw_value or "").strip()
    if not normalized:
        return None
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _is_seed_followup_stale(entry: dict, *, now: datetime | None = None) -> bool:
    updated_at = _parse_iso_timestamp(str(entry.get("updated_at") or ""))
    if updated_at is None:
        return False
    reference_time = now or datetime.now(timezone.utc)
    return (reference_time - updated_at) > MAX_PENDING_SEED_FOLLOWUP_AGE


def store_seed_followup_context(
    *,
    session: Session,
    tenant: Tenant,
    request_id: str | None,
    user_id: str,
    channel_ids: list[str],
    project_id: str,
    project_key: str,
    issue_keys: list[str],
    questions: list[object],
    prompt_markdown: str,
) -> str:
    normalized_channel_ids = [
        value.strip() for value in channel_ids if value and value.strip()
    ]
    normalized_issue_keys = [
        value.strip().upper() for value in issue_keys if value and value.strip()
    ]
    normalized_questions = ClarificationQuestionSet.from_values(questions).to_payload()
    normalized_request_id = (request_id or "").strip() or uuid4().hex

    metadata = {
        "request_id": normalized_request_id,
        "user_id": user_id.strip() or None,
        "channel_ids": normalized_channel_ids,
        "questions": normalized_questions,
        "issue_keys": normalized_issue_keys,
        "project_id": project_id.strip() or None,
        "project_key": project_key.strip().upper() or None,
        "prompt_markdown": prompt_markdown.strip(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    root_channel_id = normalized_channel_ids[0] if normalized_channel_ids else None
    thread_channel_id = next(
        (value for value in normalized_channel_ids if value != root_channel_id), None
    )
    upsert_followup_context(
        session=session,
        tenant_id=tenant.tenant_id,
        project_id=project_id.strip() or None,
        context_type=FOLLOWUP_CONTEXT_SEED_FOLLOWUP,
        channel_id=root_channel_id,
        thread_channel_id=thread_channel_id,
        owner_user_id=user_id.strip() or None,
        origin_command="issues",
        request_id=normalized_request_id,
        metadata=metadata,
    )
    return normalized_request_id


def find_seed_followup_context(
    *,
    session: Session,
    tenant: Tenant,
    channel_id: str,
    user_id: str | None = None,
    project_key: str | None = None,
) -> dict | None:
    normalized_channel_id = channel_id.strip()
    if not normalized_channel_id:
        return None
    now = datetime.now(timezone.utc)
    rows = (
        session.execute(
            select(FollowupContext)
            .where(
                FollowupContext.tenant_id == tenant.tenant_id,
                FollowupContext.context_type == FOLLOWUP_CONTEXT_SEED_FOLLOWUP,
                FollowupContext.status == "active",
            )
            .order_by(FollowupContext.updated_at.desc())
        )
        .scalars()
        .all()
    )
    active_entries: list[dict] = []
    for row in rows:
        entry = dict(row.metadata_json or {})
        entry.setdefault("request_id", row.request_id)
        entry.setdefault("project_id", row.project_id)
        channel_ids = [
            value
            for value in (
                str(row.channel_id or "").strip(),
                str(row.thread_channel_id or "").strip(),
            )
            if value
        ]
        if channel_ids:
            entry["channel_ids"] = channel_ids
        if _is_seed_followup_stale(entry, now=now):
            continue
        active_entries.append(entry)
        if not isinstance(channel_ids, list):
            continue
        if normalized_channel_id in channel_ids:
            return entry

    normalized_user_id = str(user_id or "").strip()
    normalized_project_key = str(project_key or "").strip().upper()
    if not normalized_user_id and not normalized_project_key:
        return None

    matching_entries: list[dict] = []
    for entry in active_entries:
        if normalized_user_id:
            entry_user_id = str(entry.get("user_id") or "").strip()
            if entry_user_id != normalized_user_id:
                continue
        if normalized_project_key:
            entry_project_key = str(entry.get("project_key") or "").strip().upper()
            if entry_project_key != normalized_project_key:
                continue
        matching_entries.append(entry)
    if len(matching_entries) == 1:
        return matching_entries[0]
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
    close_followup_contexts(
        session=session,
        tenant_id=tenant.tenant_id,
        context_type=FOLLOWUP_CONTEXT_SEED_FOLLOWUP,
        request_id=normalized_request_id,
    )


def remove_issue_key_from_seed_followups(
    *,
    session: Session,
    tenant: Tenant,
    issue_key: str,
) -> tuple[int, int]:
    normalized_issue_key = issue_key.strip().upper()
    if not normalized_issue_key:
        return 0, 0
    rows = (
        session.execute(
            select(FollowupContext)
            .where(
                FollowupContext.tenant_id == tenant.tenant_id,
                FollowupContext.context_type == FOLLOWUP_CONTEXT_SEED_FOLLOWUP,
                FollowupContext.status == "active",
            )
            .order_by(FollowupContext.updated_at.desc())
        )
        .scalars()
        .all()
    )
    removed_contexts = 0
    removed_issue_refs = 0

    for row in rows:
        entry = dict(row.metadata_json or {})
        raw_issue_keys = entry.get("issue_keys")
        issue_keys = (
            [
                str(value).strip().upper()
                for value in raw_issue_keys
                if str(value).strip()
            ]
            if isinstance(raw_issue_keys, list)
            else []
        )
        if not issue_keys:
            continue
        kept_issue_keys = [key for key in issue_keys if key != normalized_issue_key]
        removed_for_entry = len(issue_keys) - len(kept_issue_keys)
        if removed_for_entry <= 0:
            continue
        removed_issue_refs += removed_for_entry
        if not kept_issue_keys:
            removed_contexts += 1
            row.status = "closed"
            row.closed_at = datetime.now(timezone.utc)
            row.updated_at = datetime.now(timezone.utc)
            continue
        entry["issue_keys"] = kept_issue_keys
        entry["updated_at"] = datetime.now(timezone.utc).isoformat()
        row.metadata_json = entry
        row.updated_at = datetime.now(timezone.utc)
    return removed_contexts, removed_issue_refs
