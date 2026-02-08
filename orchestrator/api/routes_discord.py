from __future__ import annotations

import re
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from orchestrator.api.dependencies import get_session
from orchestrator.api.routes_admin import _jira_oauth_client, _refresh_jira_connection_tokens
from orchestrator.api.schemas import DiscordCommandRequest, DiscordCommandResponse
from orchestrator.core.codex_agents import (
    answer_board_question_with_codex,
    plan_discord_ask_intent_with_codex,
    plan_seed_issues_with_codex,
)
from orchestrator.core.codex_runtime import CodexRuntimeError, build_codex_runtime
from orchestrator.core.config import get_settings
from orchestrator.core.runs import (
    RUN_STATUS_BLOCKED,
    RUN_STATUS_CANCELLED,
    RUN_STATUS_FAILED,
    RUN_STATUS_QUEUED,
    RUN_STATUS_RUNNING,
    cancel_run,
    enqueue_run,
)
from orchestrator.storage.models import JiraOAuthConnection, Run, Tenant, WebhookDelivery
from orchestrator.tools.jira_oauth import JiraIssueCreateInput, JiraIssuePreview, JiraOAuthError

router = APIRouter(tags=["discord"])

SENSITIVE_COMMANDS = {"run", "cancel", "retry", "promote", "issues"}
PUBLIC_COMMANDS = {"help", "status", "runs", "policy", "link", "ask", "request", "bug"}
SUPPORTED_COMMANDS = SENSITIVE_COMMANDS | PUBLIC_COMMANDS
RETRYABLE_STATUSES = {RUN_STATUS_FAILED, RUN_STATUS_BLOCKED, RUN_STATUS_CANCELLED}
ISSUE_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")
REQUEST_PERMISSION_LABELS = {
    "run_controls": "run controls (!run, !cancel, !retry)",
    "seed_issues": "issue seeding (!issues seed)",
    "all_sensitive": "all sensitive commands",
}
MAX_PENDING_ASK_ACTIONS = 50
MAX_ASK_HISTORY_ENTRIES = 80
MAX_ASK_HISTORY_CONTEXT = 6


def _normalize_status_name(value: str) -> str:
    return value.strip().lower()


def _parse_command_text(command_text: str) -> tuple[str, list[str]]:
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


def _tenant_allowlisted_user_ids(tenant: Tenant) -> set[str]:
    discord_config = tenant.discord_config or {}
    raw_allowlist = discord_config.get("allowed_user_ids")
    if not isinstance(raw_allowlist, list):
        return set()
    normalized = {str(user_id).strip() for user_id in raw_allowlist if str(user_id).strip()}
    return normalized


def _tenant_allowlist_requests(tenant: Tenant) -> list[dict]:
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


def _create_allowlist_request(
    *,
    session: Session,
    tenant: Tenant,
    user_id: str,
    channel_id: str | None,
    permissions: list[str],
    reason: str | None,
) -> tuple[bool, str]:
    allowlisted_ids = _tenant_allowlisted_user_ids(tenant)
    if user_id in allowlisted_ids:
        return False, "You are already allowlisted for sensitive commands."

    requests = _tenant_allowlist_requests(tenant)
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


def _assert_sensitive_command_permission(*, tenant: Tenant, command_name: str, user_id: str) -> None:
    if command_name not in SENSITIVE_COMMANDS:
        return
    allowlist = _tenant_allowlisted_user_ids(tenant)
    if user_id not in allowlist:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"'{command_name}' requires an allowlisted Discord user",
        )


def _assert_channel_scope(*, tenant: Tenant, channel_id: str | None) -> None:
    if not channel_id:
        return
    allowed_channel_ids = _tenant_allowed_channel_ids(tenant)
    if allowed_channel_ids and channel_id not in allowed_channel_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Command channel does not match tenant Discord channel",
        )


def _tenant_allowed_channel_ids(tenant: Tenant) -> set[str]:
    discord_config = tenant.discord_config or {}
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
    return allowed


def _format_elapsed_seconds(*, started_at: datetime | None, created_at: datetime | None) -> int:
    anchor = started_at or created_at
    if anchor is None:
        return 0
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - anchor).total_seconds()))


def _fetch_jira_issue_preview(
    *,
    session: Session,
    tenant: Tenant,
    issue_key: str,
) -> JiraIssuePreview:
    settings = get_settings()
    connection_id = str(tenant.jira_config.get("connection_id") or "").strip()
    if not connection_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Jira OAuth connection is not linked for this tenant",
        )
    connection = session.get(JiraOAuthConnection, connection_id)
    if connection is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Configured Jira connection was not found",
        )
    try:
        access_token = _refresh_jira_connection_tokens(
            session,
            connection=connection,
            settings=settings,
        )
        client = _jira_oauth_client(session=session, settings=settings)
        issues = client.search_issues_by_jql(
            access_token=access_token,
            cloud_id=connection.cloud_id,
            jql=f'key = "{issue_key}"',
            max_results=1,
        )
    except (ValueError, JiraOAuthError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to query Jira issue status: {exc}",
        ) from exc

    if not issues:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Issue {issue_key} was not found in Jira",
        )
    return issues[0]


def _search_jira_issues_for_tenant(
    *,
    session: Session,
    tenant: Tenant,
    jql: str,
    max_results: int = 20,
) -> list[JiraIssuePreview]:
    settings = get_settings()
    connection_id = str(tenant.jira_config.get("connection_id") or "").strip()
    if not connection_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Jira OAuth connection is not linked for this tenant",
        )
    connection = session.get(JiraOAuthConnection, connection_id)
    if connection is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Configured Jira connection was not found",
        )
    try:
        access_token = _refresh_jira_connection_tokens(
            session,
            connection=connection,
            settings=settings,
        )
        client = _jira_oauth_client(session=session, settings=settings)
        return client.search_issues_by_jql(
            access_token=access_token,
            cloud_id=connection.cloud_id,
            jql=jql,
            max_results=max_results,
        )
    except (ValueError, JiraOAuthError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to query Jira board: {exc}",
        ) from exc


def _ensure_issue_is_executable(*, issue_status: str, tenant: Tenant) -> None:
    executable_statuses = ["To Do"]
    configured_ready_statuses = tenant.jira_config.get("ready_statuses")
    if isinstance(configured_ready_statuses, list):
        executable_statuses.extend(
            status_name.strip()
            for status_name in (str(value) for value in configured_ready_statuses)
            if status_name.strip()
        )
    normalized_executable_statuses = {_normalize_status_name(value) for value in executable_statuses}
    if _normalize_status_name(issue_status) not in normalized_executable_statuses:
        display_statuses = ", ".join(sorted(set(executable_statuses)))
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Issue is in '{issue_status}', expected one of: {display_statuses}",
        )


def _command_help_message() -> str:
    return (
        "Commands: !help, !status, !runs [N], !run <ISSUE_KEY>, !cancel <RUN_ID>, "
        "!retry <ISSUE_KEY|RUN_ID>, !policy, !link <ISSUE_KEY>, !ask <question>, !bug <summary> [details], "
        "!issues seed <markdown spec>, !request <run_controls|seed_issues|all_sensitive> [reason]"
    )


def _command_policy_message() -> str:
    return (
        "Policy: no secrets in output, no sleep-based synchronization, tests required for behavior "
        "changes, and Decision Gate required when requirements are ambiguous."
    )


def _project_filter_jql(tenant: Tenant) -> str:
    keys = [str(key).strip().upper() for key in tenant.jira_config.get("project_keys", []) if str(key).strip()]
    if not keys:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Tenant has no Jira project keys")
    if len(keys) == 1:
        return f'project = "{keys[0]}"'
    joined = ", ".join(f'"{key}"' for key in keys)
    return f"project in ({joined})"


def _tenant_project_keys(tenant: Tenant) -> list[str]:
    return [str(key).strip().upper() for key in tenant.jira_config.get("project_keys", []) if str(key).strip()]


def _normalize_seed_issue_labels(raw_labels: object) -> list[str]:
    if not isinstance(raw_labels, list):
        return ["discord-seeded"]
    normalized: list[str] = ["discord-seeded"]
    for label in raw_labels:
        text = str(label).strip().lower()
        if text and text not in normalized:
            normalized.append(text)
    return normalized


def _normalize_seed_issue_tags(raw_tags: object) -> list[str]:
    if not isinstance(raw_tags, list):
        return []
    normalized: list[str] = []
    for tag in raw_tags:
        text = str(tag).strip().lower()
        if text and text not in normalized:
            normalized.append(text)
    return normalized


def _parse_seed_issue_type(raw_issue_type: object) -> str:
    normalized = str(raw_issue_type or "").strip().lower()
    if normalized == "bug":
        return "Bug"
    if normalized == "story":
        return "Story"
    return "Task"


def _normalize_seed_issue_scope(raw_scope: object) -> list[str]:
    if not isinstance(raw_scope, list):
        return []
    return [str(item).strip() for item in raw_scope if str(item).strip()]


def _build_seed_issue_description(
    *,
    objective: str,
    scope_in: list[str],
    scope_out: list[str],
    acceptance_criteria: list[str],
    tags: list[str],
) -> str:
    lines = ["Objective", objective.strip() or "No objective provided", "", "Scope In"]
    if scope_in:
        lines.extend(f"- {item}" for item in scope_in)
    else:
        lines.append("- Not specified")
    lines.extend(["", "Scope Out"])
    if scope_out:
        lines.extend(f"- {item}" for item in scope_out)
    else:
        lines.append("- Not specified")
    lines.extend(["", "Acceptance Criteria"])
    if acceptance_criteria:
        lines.extend(f"- {criterion}" for criterion in acceptance_criteria)
    else:
        lines.append("- Criteria were not provided")
    lines.extend(["", "Tags"])
    if tags:
        lines.extend(f"- {tag}" for tag in tags)
    else:
        lines.append("- Not specified")
    return "\n".join(lines)


def _normalize_discord_attachments(raw_attachments: object) -> list[dict[str, str]]:
    if not isinstance(raw_attachments, list):
        return []
    normalized: list[dict[str, str]] = []
    for item in raw_attachments:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        filename = str(item.get("filename") or "").strip() or "attachment"
        content_type = str(item.get("content_type") or "").strip()
        normalized.append(
            {
                "url": url,
                "filename": filename,
                "content_type": content_type,
            }
        )
    return normalized[:5]


def _build_discord_bug_description(
    *,
    summary: str,
    details: str,
    reporter_user_id: str,
    channel_id: str | None,
    related_issue_key: str | None,
    attachments: list[dict[str, str]],
) -> str:
    lines = [
        "Reported via Discord",
        f"- Reporter: {reporter_user_id}",
        f"- Channel: {channel_id or 'unknown'}",
        f"- Reported at: {datetime.now(timezone.utc).isoformat()}",
    ]
    if related_issue_key:
        lines.append(f"- Related issue: {related_issue_key}")
    lines.extend(["", "Summary", summary.strip(), "", "Context"])
    lines.append(details.strip() or "No additional context provided.")
    if attachments:
        lines.extend(["", "Attachments"])
        for attachment in attachments:
            filename = attachment.get("filename") or "attachment"
            url = attachment.get("url") or ""
            content_type = attachment.get("content_type") or ""
            if content_type:
                lines.append(f"- [{filename}]({url}) ({content_type})")
            else:
                lines.append(f"- [{filename}]({url})")
    return "\n".join(lines)


def _create_discord_bug_issue(
    *,
    session: Session,
    tenant: Tenant,
    summary: str,
    details: str,
    reporter_user_id: str,
    channel_id: str | None,
    related_issue_key: str | None,
    attachments: list[dict[str, str]],
) -> tuple[str, dict]:
    project_keys = _tenant_project_keys(tenant)
    if not project_keys:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Tenant has no Jira project keys")
    project_key = project_keys[0]
    connection_id = str(tenant.jira_config.get("connection_id") or "").strip()
    if not connection_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Jira OAuth connection is not linked for this tenant",
        )
    connection = session.get(JiraOAuthConnection, connection_id)
    if connection is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Configured Jira connection was not found",
        )

    description = _build_discord_bug_description(
        summary=summary,
        details=details,
        reporter_user_id=reporter_user_id,
        channel_id=channel_id,
        related_issue_key=related_issue_key,
        attachments=attachments,
    )
    issue_input = JiraIssueCreateInput(
        summary=summary.strip()[:90],
        description=description,
        labels=["discord-bug", "from-discord"],
        issue_type="Bug",
    )
    settings = get_settings()
    try:
        access_token = _refresh_jira_connection_tokens(
            session,
            connection=connection,
            settings=settings,
        )
        client = _jira_oauth_client(session=session, settings=settings)
        create_result = client.create_issues_bulk(
            access_token=access_token,
            cloud_id=connection.cloud_id,
            project_key=project_key,
            issues=[issue_input],
        )
    except (ValueError, JiraOAuthError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to create Jira bug: {exc}",
        ) from exc

    if not create_result.created:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Jira bug create returned no issues: {'; '.join(create_result.errors) or 'unknown error'}",
        )

    created_issue = create_result.created[0]
    browse_base_url = str(connection.site_url or "").strip().rstrip("/")
    issue_url = f"{browse_base_url}/browse/{created_issue.key}" if browse_base_url else None
    if issue_url:
        message = f"Bug logged: [{created_issue.key}]({issue_url})"
    else:
        message = f"Bug logged: {created_issue.key}"
    if create_result.errors:
        message = f"{message} (warnings: {'; '.join(create_result.errors)})"
    return (
        message,
        {
            "created_issue_keys": [created_issue.key],
            "created_issue_links": [issue_url] if issue_url else [],
            "issue_type": "Bug",
            "project_key": project_key,
        },
    )


def _collect_ask_context(
    *,
    session: Session,
    tenant: Tenant,
    question: str,
    scoped_issue_key: str | None = None,
) -> tuple[str | None, str | None, list[dict], dict[str, int]]:
    project_jql = _project_filter_jql(tenant)
    if scoped_issue_key:
        normalized_issue_key = scoped_issue_key.strip().upper()
        jira_issues = _search_jira_issues_for_tenant(
            session=session,
            tenant=tenant,
            jql=f'{project_jql} AND key = "{normalized_issue_key}"',
            max_results=1,
        )
        if not jira_issues:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Issue {normalized_issue_key} was not found for this tenant",
            )
    else:
        normalized_issue_key = None

    lowered = question.strip().lower()
    status_queries = {
        "blocked": "Blocked",
        "in progress": "In Progress",
        "to do": "To Do",
        "testing": "Testing",
        "done": "Done",
        "ready to release": "READY TO RELEASE",
    }
    requested_status = None
    for needle, status_name in status_queries.items():
        if needle in lowered:
            requested_status = status_name
            break

    if normalized_issue_key is None and requested_status:
        jira_issues = _search_jira_issues_for_tenant(
            session=session,
            tenant=tenant,
            jql=f'{project_jql} AND status = "{requested_status}" ORDER BY updated DESC',
            max_results=30,
        )
    elif normalized_issue_key is None:
        jira_issues = _search_jira_issues_for_tenant(
            session=session,
            tenant=tenant,
            jql=f"{project_jql} ORDER BY updated DESC",
            max_results=60,
        )

    issues = [
        {
            "key": issue.key,
            "summary": issue.summary,
            "status": issue.status,
        }
        for issue in jira_issues
    ]
    status_counts: dict[str, int] = {}
    for issue in issues:
        issue_status = issue["status"]
        status_counts[issue_status] = status_counts.get(issue_status, 0) + 1

    return normalized_issue_key, requested_status, issues, status_counts


def _store_pending_ask_action(
    *,
    session: Session,
    tenant: Tenant,
    user_id: str,
    channel_id: str | None,
    question: str,
    summary: str,
    proposed_command: str,
) -> dict:
    discord_config = dict(tenant.discord_config or {})
    raw_pending = discord_config.get("pending_ask_actions")
    pending = [entry for entry in raw_pending if isinstance(entry, dict)] if isinstance(raw_pending, list) else []
    request_id = uuid4().hex
    created_at = datetime.now(timezone.utc).isoformat()
    pending.append(
        {
            "request_id": request_id,
            "user_id": user_id.strip(),
            "channel_id": channel_id.strip() if isinstance(channel_id, str) and channel_id.strip() else None,
            "question": question.strip(),
            "summary": summary.strip(),
            "proposed_command": proposed_command.strip(),
            "created_at": created_at,
        }
    )
    discord_config["pending_ask_actions"] = pending[-MAX_PENDING_ASK_ACTIONS:]
    tenant.discord_config = discord_config
    tenant.updated_at = datetime.now(timezone.utc)
    session.commit()
    return {
        "request_id": request_id,
        "created_at": created_at,
    }


def consume_pending_ask_action(
    *,
    session: Session,
    tenant: Tenant,
    request_id: str,
) -> dict | None:
    normalized_request_id = request_id.strip()
    if not normalized_request_id:
        return None
    discord_config = dict(tenant.discord_config or {})
    raw_pending = discord_config.get("pending_ask_actions")
    pending = [entry for entry in raw_pending if isinstance(entry, dict)] if isinstance(raw_pending, list) else []
    matched: dict | None = None
    kept: list[dict] = []
    for entry in pending:
        entry_id = str(entry.get("request_id") or "").strip()
        if matched is None and entry_id == normalized_request_id:
            matched = entry
            continue
        kept.append(entry)
    if matched is None:
        return None
    discord_config["pending_ask_actions"] = kept
    tenant.discord_config = discord_config
    tenant.updated_at = datetime.now(timezone.utc)
    session.commit()
    return matched


def _tenant_ask_history(tenant: Tenant) -> list[dict]:
    discord_config = tenant.discord_config or {}
    raw_history = discord_config.get("ask_history")
    if not isinstance(raw_history, list):
        return []
    normalized: list[dict] = []
    for entry in raw_history:
        if not isinstance(entry, dict):
            continue
        user_id = str(entry.get("user_id") or "").strip()
        channel_id = str(entry.get("channel_id") or "").strip()
        question = str(entry.get("question") or "").strip()
        answer = str(entry.get("answer") or "").strip()
        if not user_id or not channel_id or not question or not answer:
            continue
        normalized.append(
            {
                "user_id": user_id,
                "channel_id": channel_id,
                "question": question,
                "answer": answer,
                "issue_key": str(entry.get("issue_key") or "").strip().upper() or None,
                "status": str(entry.get("status") or "").strip() or None,
                "created_at": str(entry.get("created_at") or "").strip()
                or datetime.now(timezone.utc).isoformat(),
            }
        )
    return normalized


def _recent_ask_history(
    *,
    tenant: Tenant,
    user_id: str,
    channel_id: str,
    limit: int = MAX_ASK_HISTORY_CONTEXT,
) -> list[dict]:
    entries = _tenant_ask_history(tenant)
    scoped = [
        entry
        for entry in entries
        if entry.get("user_id") == user_id and entry.get("channel_id") == channel_id
    ]
    if not scoped:
        return []
    return scoped[-max(1, limit) :]


def _store_ask_history_entry(
    *,
    session: Session,
    tenant: Tenant,
    user_id: str,
    channel_id: str,
    question: str,
    answer: str,
    issue_key: str | None,
    status_name: str | None,
) -> None:
    discord_config = dict(tenant.discord_config or {})
    entries = _tenant_ask_history(tenant)
    entries.append(
        {
            "user_id": user_id,
            "channel_id": channel_id,
            "question": question.strip(),
            "answer": answer.strip(),
            "issue_key": issue_key.strip().upper() if isinstance(issue_key, str) and issue_key.strip() else None,
            "status": status_name.strip() if isinstance(status_name, str) and status_name.strip() else None,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    discord_config["ask_history"] = entries[-MAX_ASK_HISTORY_ENTRIES:]
    tenant.discord_config = discord_config
    tenant.updated_at = datetime.now(timezone.utc)
    session.commit()


def _seed_issues_with_codex(*, session: Session, tenant: Tenant, prompt_markdown: str) -> tuple[str, dict]:
    project_keys = _tenant_project_keys(tenant)
    if not project_keys:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Tenant has no Jira project keys")

    settings = get_settings()
    runtime = build_codex_runtime(session=session, settings=settings)
    try:
        plan_payload = plan_seed_issues_with_codex(
            runtime=runtime,
            prompt_markdown=prompt_markdown,
            allowed_project_keys=project_keys,
        )
    except CodexRuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Codex issue seeding is unavailable: {exc}",
        ) from exc

    project_key = str(plan_payload.get("project_key") or project_keys[0]).strip().upper()
    if project_key not in project_keys:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Codex selected unsupported Jira project key '{project_key}'",
        )

    raw_issues = plan_payload.get("issues")
    if not isinstance(raw_issues, list):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Codex did not return issue drafts")

    issue_inputs: list[JiraIssueCreateInput] = []
    for item in raw_issues[:12]:
        if not isinstance(item, dict):
            continue
        summary = str(item.get("summary") or "").strip()
        objective = str(item.get("objective") or "").strip()
        scope_in = _normalize_seed_issue_scope(item.get("scope_in"))
        scope_out = _normalize_seed_issue_scope(item.get("scope_out"))
        acceptance_raw = item.get("acceptance_criteria")
        acceptance = (
            [str(entry).strip() for entry in acceptance_raw if str(entry).strip()]
            if isinstance(acceptance_raw, list)
            else []
        )
        tags = _normalize_seed_issue_tags(item.get("tags"))
        labels = _normalize_seed_issue_labels(item.get("labels"))
        for tag in tags:
            if tag not in labels:
                labels.append(tag)
        if not summary:
            continue
        issue_inputs.append(
            JiraIssueCreateInput(
                summary=summary[:90],
                description=_build_seed_issue_description(
                    objective=objective,
                    scope_in=scope_in,
                    scope_out=scope_out,
                    acceptance_criteria=acceptance,
                    tags=tags,
                ),
                labels=labels,
                issue_type=_parse_seed_issue_type(item.get("issue_type")),
            )
        )

    if not issue_inputs:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Codex returned no valid issue drafts")

    connection_id = str(tenant.jira_config.get("connection_id") or "").strip()
    if not connection_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Jira OAuth connection is not linked for this tenant",
        )
    connection = session.get(JiraOAuthConnection, connection_id)
    if connection is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Configured Jira connection was not found",
        )
    try:
        access_token = _refresh_jira_connection_tokens(
            session,
            connection=connection,
            settings=settings,
        )
        client = _jira_oauth_client(session=session, settings=settings)
        create_result = client.create_issues_bulk(
            access_token=access_token,
            cloud_id=connection.cloud_id,
            project_key=project_key,
            issues=issue_inputs,
        )
    except (ValueError, JiraOAuthError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to create Jira issues: {exc}",
        ) from exc

    created_keys = [issue.key for issue in create_result.created]
    if not created_keys:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Jira bulk create returned no issues: {'; '.join(create_result.errors) or 'unknown error'}",
        )
    browse_base_url = str(connection.site_url or "").strip().rstrip("/")
    if browse_base_url:
        created_links = [f"[{issue_key}]({browse_base_url}/browse/{issue_key})" for issue_key in created_keys]
        message = f"Seeded {len(created_keys)} issue(s): {', '.join(created_links)}"
    else:
        message = f"Seeded {len(created_keys)} issue(s): {', '.join(created_keys)}"
    if create_result.errors:
        message = f"{message} (partial errors: {'; '.join(create_result.errors)})"
    return (
        message,
        {
            "project_key": project_key,
            "created_issue_keys": created_keys,
            "created_issue_links": [
                f"{browse_base_url}/browse/{issue_key}" for issue_key in created_keys if browse_base_url
            ],
            "errors": create_result.errors,
        },
    )


def _ask_board_message(
    *,
    session: Session,
    tenant: Tenant,
    user_id: str,
    channel_id: str,
    question: str,
    scoped_issue_key: str | None = None,
) -> tuple[str, dict]:
    history_context = _recent_ask_history(
        tenant=tenant,
        user_id=user_id,
        channel_id=channel_id,
        limit=MAX_ASK_HISTORY_CONTEXT,
    )
    resolved_scoped_issue_key = scoped_issue_key
    if resolved_scoped_issue_key is None:
        for entry in reversed(history_context):
            issue_key = str(entry.get("issue_key") or "").strip().upper()
            if issue_key:
                resolved_scoped_issue_key = issue_key
                break

    normalized_issue_key, requested_status, issues, status_counts = _collect_ask_context(
        session=session,
        tenant=tenant,
        question=question,
        scoped_issue_key=resolved_scoped_issue_key,
    )

    settings = get_settings()
    runtime = build_codex_runtime(session=session, settings=settings)
    try:
        message = answer_board_question_with_codex(
            runtime=runtime,
            question=question,
            project_keys=[str(key).strip().upper() for key in tenant.jira_config.get("project_keys", []) if str(key).strip()],
            issues=issues,
            status_counts=status_counts,
            history=history_context,
        )
    except CodexRuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Codex board assistant is unavailable: {exc}",
        ) from exc

    _store_ask_history_entry(
        session=session,
        tenant=tenant,
        user_id=user_id,
        channel_id=channel_id,
        question=question,
        answer=message,
        issue_key=normalized_issue_key,
        status_name=requested_status,
    )

    return (
        message,
        {
            "issue_key": normalized_issue_key,
            "status": requested_status,
            "status_counts": status_counts,
            "issues": issues,
            "question": question,
            "memory_entries_used": len(history_context),
        },
    )


@router.post("/discord/command/{tenant_id}", response_model=DiscordCommandResponse)
def execute_discord_command(
    tenant_id: str,
    payload: DiscordCommandRequest,
    session: Session = Depends(get_session),
    *,
    defer_seed_issues: bool = False,
    require_ask_confirmation: bool = False,
    allow_plain_ask: bool = False,
) -> DiscordCommandResponse:
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown tenant")
    if not tenant.is_enabled:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Tenant is disabled")

    _assert_channel_scope(tenant=tenant, channel_id=payload.channel_id)
    raw_command = payload.command.strip()
    command_text = raw_command
    if allow_plain_ask and raw_command and not raw_command.startswith("!"):
        command_text = f"!ask {raw_command}"
    command_name, arguments = _parse_command_text(command_text)
    _assert_sensitive_command_permission(tenant=tenant, command_name=command_name, user_id=payload.user_id)

    if command_name == "help":
        return DiscordCommandResponse(ok=True, command=command_name, message=_command_help_message(), data=None)

    if command_name == "policy":
        return DiscordCommandResponse(ok=True, command=command_name, message=_command_policy_message(), data=None)

    if command_name == "status":
        queued_count = int(
            session.execute(
                select(func.count(Run.run_id)).where(
                    Run.tenant_id == tenant_id,
                    Run.status == RUN_STATUS_QUEUED,
                )
            ).scalar_one()
        )
        active_runs = session.execute(
            select(Run)
            .where(
                Run.tenant_id == tenant_id,
                Run.status == RUN_STATUS_RUNNING,
            )
            .order_by(Run.started_at.asc())
        ).scalars().all()
        last_webhook_seen = session.execute(
            select(func.max(WebhookDelivery.created_at)).where(WebhookDelivery.tenant_id == tenant_id)
        ).scalar_one()
        active_payload = [
            {
                "run_id": run.run_id,
                "issue_key": run.issue_key,
                "status": run.status,
                "elapsed_seconds": _format_elapsed_seconds(
                    started_at=run.started_at,
                    created_at=run.created_at,
                ),
            }
            for run in active_runs
        ]
        message = (
            f"Tenant {'enabled' if tenant.is_enabled else 'disabled'}; "
            f"queue_depth={queued_count}; active_runs={len(active_payload)}"
        )
        return DiscordCommandResponse(
            ok=True,
            command=command_name,
            message=message,
            data={
                "webhook_last_seen": last_webhook_seen.isoformat() if last_webhook_seen else None,
                "queue_depth": queued_count,
                "active_runs": active_payload,
            },
        )

    if command_name == "runs":
        limit = 10
        if arguments:
            try:
                limit = min(max(1, int(arguments[0])), 50)
            except ValueError as exc:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid run limit") from exc

        runs = session.execute(
            select(Run)
            .where(Run.tenant_id == tenant_id)
            .order_by(Run.created_at.desc())
            .limit(limit)
        ).scalars().all()
        run_payload = [
            {
                "run_id": run.run_id,
                "issue_key": run.issue_key,
                "status": run.status,
                "pr_url": run.pr_url,
                "created_at": run.created_at.isoformat() if run.created_at else None,
            }
            for run in runs
        ]
        return DiscordCommandResponse(
            ok=True,
            command=command_name,
            message=f"Returned {len(run_payload)} run(s)",
            data={"runs": run_payload},
        )

    if command_name == "link":
        if len(arguments) != 1:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Usage: !link <ISSUE_KEY>")
        issue_key = arguments[0].strip().upper()
        latest_pr = session.execute(
            select(Run)
            .where(
                Run.tenant_id == tenant_id,
                Run.issue_key == issue_key,
                Run.pr_url.is_not(None),
            )
            .order_by(Run.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        jira_base = "https://example.atlassian.net"
        jira_link = f"{jira_base}/browse/{issue_key}"
        return DiscordCommandResponse(
            ok=True,
            command=command_name,
            message=f"Links for {issue_key}",
            data={
                "issue_key": issue_key,
                "jira_url": jira_link,
                "pr_url": latest_pr.pr_url if latest_pr else None,
            },
        )

    if command_name == "ask":
        if not arguments:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Usage: !ask <question> or !ask @ISSUE-123 <question>",
            )
        scoped_issue_key: str | None = None
        question_tokens = arguments
        first_token = arguments[0].strip()
        if first_token.startswith("@"):
            candidate_issue_key = first_token[1:].strip().upper()
            if not ISSUE_KEY_PATTERN.match(candidate_issue_key):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Usage: !ask @ISSUE-123 <question>",
                )
            scoped_issue_key = candidate_issue_key
            question_tokens = arguments[1:]
        question = " ".join(question_tokens).strip()
        if not question:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Usage: !ask <question> or !ask @ISSUE-123 <question>",
            )
        normalized_user_id = payload.user_id.strip()
        normalized_channel_id = payload.channel_id.strip() if payload.channel_id else "__dm__"
        if require_ask_confirmation:
            history_context = _recent_ask_history(
                tenant=tenant,
                user_id=normalized_user_id,
                channel_id=normalized_channel_id,
                limit=MAX_ASK_HISTORY_CONTEXT,
            )
            resolved_scoped_issue_key = scoped_issue_key
            if resolved_scoped_issue_key is None:
                for entry in reversed(history_context):
                    issue_key_from_history = str(entry.get("issue_key") or "").strip().upper()
                    if issue_key_from_history:
                        resolved_scoped_issue_key = issue_key_from_history
                        break
            normalized_issue_key, requested_status, issues, status_counts = _collect_ask_context(
                session=session,
                tenant=tenant,
                question=question,
                scoped_issue_key=resolved_scoped_issue_key,
            )
            settings = get_settings()
            runtime = build_codex_runtime(session=session, settings=settings)
            try:
                intent_payload = plan_discord_ask_intent_with_codex(
                    runtime=runtime,
                    question=question,
                    project_keys=[
                        str(key).strip().upper()
                        for key in tenant.jira_config.get("project_keys", [])
                        if str(key).strip()
                    ],
                    issues=issues,
                    status_counts=status_counts,
                    history=history_context,
                )
            except CodexRuntimeError as exc:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=f"Codex board assistant is unavailable: {exc}",
                ) from exc

            mode = str(intent_payload.get("mode") or "").strip().lower()
            summary = str(intent_payload.get("summary") or "").strip()
            proposed_command = str(intent_payload.get("command") or "").strip()
            if mode == "command" and proposed_command.startswith("!") and not proposed_command.lower().startswith("!ask"):
                pending = _store_pending_ask_action(
                    session=session,
                    tenant=tenant,
                    user_id=normalized_user_id,
                    channel_id=payload.channel_id,
                    question=question,
                    summary=summary or "Proposed operational action from /ask",
                    proposed_command=proposed_command,
                )
                confirmation_message = summary or "I can run this action for you after approval."
                return DiscordCommandResponse(
                    ok=True,
                    command=command_name,
                    message=confirmation_message,
                    data={
                        "requires_confirmation": True,
                        "request_id": pending["request_id"],
                        "proposed_command": proposed_command,
                        "summary": confirmation_message,
                    },
                )

            message = answer_board_question_with_codex(
                runtime=runtime,
                question=question,
                project_keys=[
                    str(key).strip().upper()
                    for key in tenant.jira_config.get("project_keys", [])
                    if str(key).strip()
                ],
                issues=issues,
                status_counts=status_counts,
                history=history_context,
            )
            _store_ask_history_entry(
                session=session,
                tenant=tenant,
                user_id=normalized_user_id,
                channel_id=normalized_channel_id,
                question=question,
                answer=message,
                issue_key=normalized_issue_key,
                status_name=requested_status,
            )
            return DiscordCommandResponse(
                ok=True,
                command=command_name,
                message=message,
                data={
                    "issue_key": normalized_issue_key,
                    "status": requested_status,
                    "status_counts": status_counts,
                    "issues": issues,
                    "question": question,
                },
            )

        message, data = _ask_board_message(
            session=session,
            tenant=tenant,
            user_id=normalized_user_id,
            channel_id=normalized_channel_id,
            question=question,
            scoped_issue_key=scoped_issue_key,
        )
        return DiscordCommandResponse(
            ok=True,
            command=command_name,
            message=message,
            data=data,
        )

    if command_name == "bug":
        command_params = payload.command_params if isinstance(payload.command_params, dict) else {}
        summary = str(command_params.get("summary") or "").strip()
        details = str(command_params.get("details") or "").strip()
        related_issue_key_raw = str(command_params.get("issue_key") or "").strip().upper()
        related_issue_key = related_issue_key_raw if ISSUE_KEY_PATTERN.match(related_issue_key_raw) else None
        if not summary:
            raw_body = " ".join(arguments).strip()
            if " -- " in raw_body:
                summary, details_tail = raw_body.split(" -- ", 1)
                summary = summary.strip()
                if not details:
                    details = details_tail.strip()
            else:
                summary = raw_body
        if not summary:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Usage: !bug <summary> [-- details]",
            )
        attachments = _normalize_discord_attachments(payload.attachments)
        message, data = _create_discord_bug_issue(
            session=session,
            tenant=tenant,
            summary=summary,
            details=details,
            reporter_user_id=payload.user_id.strip(),
            channel_id=payload.channel_id,
            related_issue_key=related_issue_key,
            attachments=attachments,
        )
        return DiscordCommandResponse(
            ok=True,
            command=command_name,
            message=message,
            data=data,
        )

    if command_name == "issues":
        if len(arguments) < 2 or arguments[0].strip().lower() != "seed":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Usage: !issues seed <markdown spec>",
            )
        prompt_markdown = " ".join(arguments[1:]).strip()
        if not prompt_markdown:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Usage: !issues seed <markdown spec>",
            )
        if defer_seed_issues:
            return DiscordCommandResponse(
                ok=True,
                command=command_name,
                message="Issue seeding started. I will reply in this thread with created issue links when done.",
                data={"deferred": True, "prompt_markdown": prompt_markdown},
            )
        message, data = _seed_issues_with_codex(
            session=session,
            tenant=tenant,
            prompt_markdown=prompt_markdown,
        )
        return DiscordCommandResponse(
            ok=True,
            command=command_name,
            message=message,
            data=data,
        )

    if command_name == "request":
        if not arguments:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Usage: !request <run_controls|seed_issues|all_sensitive> [reason]",
            )
        permission = arguments[0].strip().lower()
        reason = " ".join(arguments[1:]).strip() or None
        if permission not in REQUEST_PERMISSION_LABELS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Permission must be one of: run_controls, seed_issues, all_sensitive",
            )

        _, message = _create_allowlist_request(
            session=session,
            tenant=tenant,
            user_id=payload.user_id.strip(),
            channel_id=payload.channel_id.strip() if payload.channel_id else None,
            permissions=[permission],
            reason=reason,
        )
        suffix = f" Requested permission: {REQUEST_PERMISSION_LABELS[permission]}."
        return DiscordCommandResponse(
            ok=True,
            command=command_name,
            message=f"{message}{suffix}",
            data={"user_id": payload.user_id.strip(), "requested": True, "permission": permission},
        )

    if command_name == "run":
        if len(arguments) != 1:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Usage: !run <ISSUE_KEY>")
        issue_key = arguments[0].strip().upper()
        issue_preview = _fetch_jira_issue_preview(session=session, tenant=tenant, issue_key=issue_key)
        _ensure_issue_is_executable(issue_status=issue_preview.status, tenant=tenant)
        enqueue_result = enqueue_run(
            session,
            tenant_id=tenant_id,
            issue_key=issue_key,
            issue_summary=issue_preview.summary,
            issue_description=None,
            delivery_id=None,
            max_concurrent_runs=tenant.policy_config.get("max_concurrent_runs"),
        )
        if not enqueue_result.enqueued:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Run could not be queued: {enqueue_result.reason}",
            )
        return DiscordCommandResponse(
            ok=True,
            command=command_name,
            message=f"Queued run {enqueue_result.run.run_id} for {issue_key}",
            data={"run_id": enqueue_result.run.run_id, "issue_key": issue_key},
        )

    if command_name == "cancel":
        if len(arguments) != 1:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Usage: !cancel <RUN_ID>")
        run_id = arguments[0].strip()
        run = session.get(Run, run_id)
        if run is None or run.tenant_id != tenant_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Run {run_id} was not found")
        cancelled = cancel_run(session, run_id=run_id, cancelled_by=payload.user_id)
        return DiscordCommandResponse(
            ok=True,
            command=command_name,
            message=f"Cancelled run {cancelled.run_id}",
            data={"run_id": cancelled.run_id, "status": cancelled.status},
        )

    if command_name == "retry":
        if len(arguments) != 1:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Usage: !retry <ISSUE_KEY|RUN_ID>")
        target = arguments[0].strip()
        run = session.get(Run, target)
        if run is None:
            issue_key = target.upper()
            run = session.execute(
                select(Run)
                .where(Run.tenant_id == tenant_id, Run.issue_key == issue_key)
                .order_by(Run.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()
        if run is None or run.tenant_id != tenant_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No run was found for '{target}'",
            )
        if run.status not in RETRYABLE_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Run {run.run_id} is {run.status}; only failed/blocked/cancelled runs can be retried",
            )
        issue_preview = _fetch_jira_issue_preview(session=session, tenant=tenant, issue_key=run.issue_key)
        _ensure_issue_is_executable(issue_status=issue_preview.status, tenant=tenant)
        enqueue_result = enqueue_run(
            session,
            tenant_id=tenant_id,
            issue_key=run.issue_key,
            issue_summary=issue_preview.summary,
            issue_description=run.issue_description,
            delivery_id=None,
            max_concurrent_runs=tenant.policy_config.get("max_concurrent_runs"),
        )
        if not enqueue_result.enqueued:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Retry could not be queued: {enqueue_result.reason}",
            )
        return DiscordCommandResponse(
            ok=True,
            command=command_name,
            message=f"Queued retry run {enqueue_result.run.run_id} for {run.issue_key}",
            data={"run_id": enqueue_result.run.run_id, "issue_key": run.issue_key},
        )

    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported command")
