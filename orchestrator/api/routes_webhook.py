from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.request import Request as UrlRequest, urlopen
from uuid import uuid4

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.api.discord_followup_format import (
    build_ask_confirmation_components,
    build_command_followup_message,
    resolve_tenant_jira_browse_base_url,
)
from orchestrator.api.webhook_payload_utils import (
    extract_webhook_token as _extract_webhook_token,
    read_json_payload as _read_json_payload,
)
from orchestrator.api.discord_ask_context import (
    consume_pending_ask_action,
    project_filter_jql as _project_filter_jql,
    remove_issue_key_from_tenant_ask_history,
    search_jira_issues_for_tenant as _search_jira_issues_for_tenant,
)
from orchestrator.api.discord_state_repository import resolve_project_for_discord_channel
from orchestrator.api.dependencies import get_session
from orchestrator.api.jira_oauth_service import jira_oauth_client as _jira_oauth_client
from orchestrator.api.jira_oauth_service import refresh_jira_connection_tokens as _refresh_jira_connection_tokens
from orchestrator.api.command_entrypoint import execute_tenant_jira_comment_command
from orchestrator.api.discord_reply_transport import DiscordReplyTransport
from orchestrator.api.schemas import DiscordCommandRequest
from orchestrator.api.webhook_followup_service import DiscordWebhookFollowupService
from orchestrator.core.config import get_settings
from orchestrator.core.project_routing import (
    find_active_project_for_issue_key,
    find_active_project_for_repo_full_name,
)
from orchestrator.core.runs import (
    RUN_STATUS_BLOCKED,
    RUN_STATUS_CANCELLED,
    RUN_STATUS_FAILED,
    enqueue_run,
)
from orchestrator.core.secret_manager import resolve_scoped_secret_ref
from orchestrator.core.signal_templates import format_discord_ready_gate_guidance
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import JiraOAuthConnection, Project, Run, Tenant
from orchestrator.tools.discord_api import DiscordApiClient, DiscordApiError
from orchestrator.tools.jira_oauth import JiraOAuthError

router = APIRouter(tags=["jira-webhook"])

# Backward-compatible alias for existing tests/patch paths while command
# execution import paths are migrated to the shared entrypoint module.
execute_discord_command = execute_tenant_jira_comment_command

logger = logging.getLogger(__name__)
GLOBAL_GITHUB_WEBHOOK_SECRET_REF = "GITHUB_WEBHOOK_SECRET"
DISCORD_INTERACTIONS_PUBLIC_KEY_SECRET_REF = "DISCORD_INTERACTIONS_PUBLIC_KEY"
SUPPORTED_JIRA_COMMENT_COMMANDS = {"run", "retry", "ask"}
JIRA_COMMENT_EVENTS = {"comment_created", "comment_updated"}
ISSUE_KEY_PATTERN = re.compile(r"\b[A-Z][A-Z0-9_]+-\d+\b")
ASK_CONFIRM_CUSTOM_ID_PATTERN = re.compile(r"^ask\.(approve|reject)\.([0-9a-f]{32})$")
ASK_REPLY_MODAL_CUSTOM_ID_PATTERN = re.compile(r"^ask\.reply\.([0-9]{15,25})$")
ASK_REPLY_OPEN_CUSTOM_ID = "ask.reply.open"


@dataclass
class JiraWebhookContext:
    request_id: str
    tenant_id: str
    tenant: Tenant
    payload: dict
    webhook_event: str | None
    issue_key: str
    issue_status: str | None
    issue_status_category_key: str | None
    issue_summary: str | None
    issue_description: str | None
    comment_command: str | None
    comment_command_argument: str | None
    comment_command_error: str | None
    delivery_id: str | None
    project: Project | None


def _normalize_jira_webhook_event(raw_value: object) -> str | None:
    if not isinstance(raw_value, str):
        return None
    normalized = raw_value.strip().lower()
    if not normalized:
        return None
    if normalized.startswith("jira:"):
        normalized = normalized[len("jira:") :]
    return normalized


def _adf_to_text(node: object) -> str:
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return " ".join(part for part in (_adf_to_text(item) for item in node) if part).strip()
    if not isinstance(node, dict):
        return ""

    text = node.get("text")
    if isinstance(text, str):
        return text

    content = node.get("content")
    if isinstance(content, list):
        return " ".join(part for part in (_adf_to_text(item) for item in content) if part).strip()
    return ""


def _extract_issue_payload(
    payload: dict,
) -> tuple[str, list[str], str | None, str | None, str | None, str | None]:
    issue = payload.get("issue")
    if not isinstance(issue, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing issue object")

    issue_key = issue.get("key")
    if not isinstance(issue_key, str) or not issue_key:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing issue key")

    fields = issue.get("fields") if isinstance(issue.get("fields"), dict) else {}
    labels = fields.get("labels") if isinstance(fields, dict) else []
    if not isinstance(labels, list):
        labels = []

    summary_raw = fields.get("summary") if isinstance(fields, dict) else None
    summary = str(summary_raw).strip() if isinstance(summary_raw, str) and summary_raw.strip() else None

    description_raw = fields.get("description") if isinstance(fields, dict) else None
    description_text = _adf_to_text(description_raw).strip()
    description = description_text or None

    status_name: str | None = None
    status_category_key: str | None = None
    status_field = fields.get("status")
    if isinstance(status_field, dict):
        raw_status_name = status_field.get("name")
        if isinstance(raw_status_name, str):
            normalized_status_name = raw_status_name.strip()
            if normalized_status_name:
                status_name = normalized_status_name

        status_category = status_field.get("statusCategory")
        if isinstance(status_category, dict):
            raw_status_category_key = status_category.get("key")
            if isinstance(raw_status_category_key, str):
                normalized_status_category_key = raw_status_category_key.strip().lower()
                if normalized_status_category_key:
                    status_category_key = normalized_status_category_key

    normalized_labels = [str(label) for label in labels]
    return issue_key, normalized_labels, status_name, status_category_key, summary, description


def _extract_jira_comment_text(payload: dict) -> str | None:
    comment = payload.get("comment")
    if not isinstance(comment, dict):
        return None
    body = comment.get("body")
    if isinstance(body, str):
        text = body.strip()
        return text or None
    if isinstance(body, dict):
        text = _adf_to_text(body).strip()
        return text or None
    return None


def _parse_jira_comment_command(payload: dict) -> tuple[str | None, str | None, str | None]:
    comment_text = _extract_jira_comment_text(payload)
    if not comment_text:
        return None, None, None

    first_non_empty_line = ""
    remaining_lines: list[str] = []
    line_index = -1
    for raw_line in comment_text.splitlines():
        line_index += 1
        candidate = raw_line.strip()
        if candidate:
            first_non_empty_line = candidate
            remaining_lines = [line.strip() for line in comment_text.splitlines()[line_index + 1 :] if line.strip()]
            break
    if not first_non_empty_line:
        return None, None, None
    if not first_non_empty_line.lower().startswith("/mb"):
        return None, None, None

    command_payload = first_non_empty_line[3:].strip()
    if not command_payload:
        return None, None, "invalid_comment_command"

    parts = [part for part in command_payload.split(" ") if part]
    if not parts:
        return None, None, "invalid_comment_command"

    command_name = parts[0].strip().lower()
    if command_name not in SUPPORTED_JIRA_COMMENT_COMMANDS:
        return None, None, "invalid_comment_command"

    if command_name in {"run", "retry"}:
        if len(parts) != 1:
            return None, None, "invalid_comment_command"
        return command_name, None, None

    if command_name == "ask":
        inline_question = " ".join(parts[1:]).strip()
        full_question = "\n".join(part for part in [inline_question, *remaining_lines] if part).strip()
        if not full_question:
            return None, None, "invalid_comment_command"
        return command_name, full_question, None

    return None, None, "invalid_comment_command"


def _extract_jira_comment_author_account_id(payload: dict) -> str | None:
    comment = payload.get("comment")
    if not isinstance(comment, dict):
        return None
    author = comment.get("author")
    if not isinstance(author, dict):
        return None
    account_id = author.get("accountId")
    if isinstance(account_id, str) and account_id.strip():
        return account_id.strip()
    return None


def _post_jira_comment(
    *,
    session: Session,
    tenant: Tenant,
    issue_key: str,
    comment: str,
    settings,  # noqa: ANN001
) -> tuple[bool, str | None]:
    connection_id = str(tenant.jira_config.get("connection_id") or "").strip()
    if not connection_id:
        return False, "Tenant Jira connection is missing"
    connection = session.get(JiraOAuthConnection, connection_id)
    if connection is None:
        return False, "Tenant Jira connection was not found"
    try:
        access_token = _refresh_jira_connection_tokens(
            session,
            connection=connection,
            settings=settings,
        )
        client = _jira_oauth_client(session=session, settings=settings)
        client.add_issue_comment(
            access_token=access_token,
            cloud_id=connection.cloud_id,
            issue_id_or_key=issue_key,
            comment=comment,
        )
        return True, None
    except (JiraOAuthError, ValueError) as exc:
        return False, str(exc)


def _extract_status_transition(payload: dict) -> tuple[str | None, str | None]:
    changelog = payload.get("changelog")
    if not isinstance(changelog, dict):
        return None, None

    items = changelog.get("items")
    if not isinstance(items, list):
        return None, None

    for item in items:
        if not isinstance(item, dict):
            continue
        field = item.get("field")
        if not isinstance(field, str) or field.strip().lower() != "status":
            continue

        from_status = item.get("fromString")
        to_status = item.get("toString")
        normalized_from_status = from_status.strip() if isinstance(from_status, str) and from_status.strip() else None
        normalized_to_status = to_status.strip() if isinstance(to_status, str) and to_status.strip() else None
        return normalized_from_status, normalized_to_status

    return None, None


def _extract_delivery_id(request: Request) -> str | None:
    header_candidates = (
        "X-Atlassian-Webhook-Identifier",
        "X-Webhook-Delivery",
        "X-GitHub-Delivery",
    )
    for header_name in header_candidates:
        value = request.headers.get(header_name)
        if value:
            normalized = value.strip()
            if normalized:
                return normalized
    return None


def _resolve_discord_interactions_public_key(
    *,
    session: Session,
    settings,
) -> bytes:  # noqa: ANN001
    raw_public_key = (
        resolve_scoped_secret_ref(
            session,
            secret_ref=DISCORD_INTERACTIONS_PUBLIC_KEY_SECRET_REF,
            encryption_key=settings.secrets_encryption_key,
        )
        or ""
    ).strip()
    if not raw_public_key:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "Discord interactions public key is missing. "
                f"Set managed secret '{DISCORD_INTERACTIONS_PUBLIC_KEY_SECRET_REF}'."
            ),
        )

    try:
        public_key_bytes = bytes.fromhex(raw_public_key)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Discord interactions public key must be a hex-encoded Ed25519 public key",
        ) from exc

    if len(public_key_bytes) != 32:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Discord interactions public key must be 32 bytes (64 hex chars)",
        )
    return public_key_bytes


def _validate_discord_interaction_signature(
    *,
    request: Request,
    payload_bytes: bytes,
    public_key: bytes,
) -> None:
    signature_hex = (request.headers.get("X-Signature-Ed25519") or "").strip()
    timestamp = (request.headers.get("X-Signature-Timestamp") or "").strip()
    if not signature_hex or not timestamp:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Discord interaction signature headers",
        )

    try:
        signature = bytes.fromhex(signature_hex)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Discord interaction signature",
        ) from exc

    message = timestamp.encode("utf-8") + payload_bytes
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, message)
    except InvalidSignature as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Discord interaction signature",
        ) from exc


def _discord_interaction_response(*, content: str, ephemeral: bool = True) -> JSONResponse:
    response_data = {"content": content}
    if ephemeral:
        response_data["flags"] = 64
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"type": 4, "data": response_data},
    )


def _discord_interaction_deferred_response(*, ephemeral: bool = True) -> JSONResponse:
    response_data: dict[str, object] = {}
    if ephemeral:
        response_data["flags"] = 64
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"type": 5, "data": response_data},
    )


def _discord_autocomplete_response(*, choices: list[dict]) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"type": 8, "data": {"choices": choices[:25]}},
    )


def _find_tenant_for_discord_channel(
    *,
    session: Session,
    channel_id: str,
) -> Tenant | None:
    tenants = session.execute(select(Tenant).where(Tenant.is_enabled.is_(True))).scalars().all()
    active_projects = session.execute(select(Project).where(Project.is_archived.is_(False))).scalars().all()
    project_channel_ids_by_tenant: dict[str, set[str]] = {}
    for project in active_projects:
        tenant_channels = project_channel_ids_by_tenant.setdefault(project.tenant_id, set())
        discord_config = dict(project.discord_config or {})
        configured_channel_id = str(discord_config.get("channel_id") or "").strip()
        if configured_channel_id:
            tenant_channels.add(configured_channel_id)
        raw_thread_ids = discord_config.get("ask_thread_channel_ids")
        if isinstance(raw_thread_ids, list):
            for value in raw_thread_ids:
                normalized = str(value or "").strip()
                if normalized:
                    tenant_channels.add(normalized)
        raw_seed_thread_ids = discord_config.get("seed_followup_thread_channel_ids")
        if isinstance(raw_seed_thread_ids, list):
            for value in raw_seed_thread_ids:
                normalized = str(value or "").strip()
                if normalized:
                    tenant_channels.add(normalized)
    matches: list[Tenant] = []
    for tenant in tenants:
        if channel_id in _tenant_discord_channel_ids(
            tenant=tenant,
            project_channel_ids=project_channel_ids_by_tenant.get(tenant.tenant_id, set()),
        ):
            matches.append(tenant)
    if len(matches) != 1:
        return None
    return matches[0]


def _tenant_discord_channel_ids(*, tenant: Tenant, project_channel_ids: set[str]) -> set[str]:
    discord_config = tenant.discord_config or {}
    channel_ids: set[str] = set(project_channel_ids)
    configured_channel_id = str(discord_config.get("channel_id") or "").strip()
    if configured_channel_id:
        channel_ids.add(configured_channel_id)
    return channel_ids


def _resolve_project_for_channel(
    *,
    session: Session,
    tenant: Tenant,
    channel_id: str,
) -> Project | None:
    return resolve_project_for_discord_channel(
        session=session,
        tenant_id=tenant.tenant_id,
        channel_id=channel_id,
    )


def _project_channel_ids_for_tenant(*, session: Session, tenant_id: str) -> set[str]:
    projects = session.execute(
        select(Project).where(
            Project.tenant_id == tenant_id,
            Project.is_archived.is_(False),
        )
    ).scalars().all()
    channel_ids: set[str] = set()
    for project in projects:
        discord_config = dict(project.discord_config or {})
        configured_channel_id = str(discord_config.get("channel_id") or "").strip()
        if configured_channel_id:
            channel_ids.add(configured_channel_id)
        raw_thread_ids = discord_config.get("ask_thread_channel_ids")
        if isinstance(raw_thread_ids, list):
            for value in raw_thread_ids:
                normalized = str(value or "").strip()
                if normalized:
                    channel_ids.add(normalized)
        raw_seed_thread_ids = discord_config.get("seed_followup_thread_channel_ids")
        if isinstance(raw_seed_thread_ids, list):
            for value in raw_seed_thread_ids:
                normalized = str(value or "").strip()
                if normalized:
                    channel_ids.add(normalized)
    return channel_ids


def _flatten_discord_option_values(options: object) -> list[str]:
    if not isinstance(options, list):
        return []
    flattened: list[str] = []
    for option in options:
        if not isinstance(option, dict):
            continue
        option_type = option.get("type")
        if option_type in {1, 2}:  # SUB_COMMAND / SUB_COMMAND_GROUP
            nested_name = option.get("name")
            if isinstance(nested_name, str) and nested_name.strip():
                flattened.append(nested_name.strip())
            flattened.extend(_flatten_discord_option_values(option.get("options")))
            continue
        value = option.get("value")
        if value is None:
            continue
        flattened.append(str(value))
    return flattened


def _find_focused_discord_option(options: object) -> tuple[str, str] | None:
    if not isinstance(options, list):
        return None
    for option in options:
        if not isinstance(option, dict):
            continue
        option_type = option.get("type")
        if option_type in {1, 2}:  # SUB_COMMAND / SUB_COMMAND_GROUP
            focused = _find_focused_discord_option(option.get("options"))
            if focused is not None:
                return focused
            continue
        if option.get("focused") is True:
            name = str(option.get("name") or "").strip()
            value = str(option.get("value") or "").strip()
            if name:
                return name, value
    return None


def _discord_option_value(options: object, *, name: str) -> str | None:
    if not isinstance(options, list):
        return None
    for option in options:
        if not isinstance(option, dict):
            continue
        option_type = option.get("type")
        if option_type in {1, 2}:  # SUB_COMMAND / SUB_COMMAND_GROUP
            nested = _discord_option_value(option.get("options"), name=name)
            if nested is not None:
                return nested
            continue
        option_name = str(option.get("name") or "").strip()
        if option_name != name:
            continue
        value = option.get("value")
        if value is None:
            return None
        return str(value).strip()
    return None


def _discord_option_attachment_ids(options: object) -> list[str]:
    if not isinstance(options, list):
        return []
    attachment_ids: list[str] = []
    for option in options:
        if not isinstance(option, dict):
            continue
        option_type = option.get("type")
        if option_type in {1, 2}:  # SUB_COMMAND / SUB_COMMAND_GROUP
            attachment_ids.extend(_discord_option_attachment_ids(option.get("options")))
            continue
        if option_type != 11:  # ATTACHMENT
            continue
        value = str(option.get("value") or "").strip()
        if value:
            attachment_ids.append(value)
    return attachment_ids


def _discord_resolved_attachments(*, data: dict, attachment_ids: list[str]) -> list[dict[str, str]]:
    if not attachment_ids:
        return []
    resolved = data.get("resolved")
    if not isinstance(resolved, dict):
        return []
    resolved_attachments = resolved.get("attachments")
    if not isinstance(resolved_attachments, dict):
        return []
    normalized: list[dict[str, str]] = []
    for attachment_id in attachment_ids:
        item = resolved_attachments.get(attachment_id)
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        normalized.append(
            {
                "id": str(item.get("id") or "").strip() or attachment_id,
                "url": url,
                "filename": str(item.get("filename") or "").strip(),
                "content_type": str(item.get("content_type") or "").strip(),
                "size": str(item.get("size") or "").strip(),
            }
        )
    return normalized[:5]


def _discord_issue_autocomplete_choices(
    *,
    session: Session,
    tenant: Tenant,
    channel_id: str | None,
    current_value: str,
) -> list[dict]:
    project_jql = _project_filter_jql(session=session, tenant=tenant, channel_id=channel_id)
    normalized = current_value.strip().upper()
    # Keep Jira query simple and deterministic, then filter in-process.
    # Prefix key matching with `key ~` is not consistently supported.
    jql = f"{project_jql} ORDER BY updated DESC"
    issues = _search_jira_issues_for_tenant(
        session=session,
        tenant=tenant,
        jql=jql,
        max_results=100,
    )

    choices: list[dict] = []
    seen_keys: set[str] = set()
    for issue in issues:
        issue_key = str(issue.key or "").strip().upper()
        summary = (issue.summary or "").strip()
        if normalized:
            haystack = f"{issue_key} {summary}".upper()
            if normalized not in haystack:
                continue
        if issue.key in seen_keys:
            continue
        seen_keys.add(issue.key)
        display = f"{issue.key} — {summary}" if summary else issue.key
        choices.append({"name": display[:100], "value": issue.key[:100]})
        if len(choices) >= 25:
            break
    return choices


def _parse_discord_interaction_command(payload: dict) -> tuple[str, str, str, dict[str, str] | None, list[dict[str, str]]]:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing interaction data")

    command_name = data.get("name")
    if not isinstance(command_name, str) or not command_name.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing interaction command name")

    channel_id = payload.get("channel_id")
    if not isinstance(channel_id, str) or not channel_id.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing interaction channel_id")

    user_id: str | None = None
    member = payload.get("member")
    if isinstance(member, dict):
        member_user = member.get("user")
        if isinstance(member_user, dict):
            raw_user_id = member_user.get("id")
            if isinstance(raw_user_id, str) and raw_user_id.strip():
                user_id = raw_user_id.strip()
    if user_id is None:
        direct_user = payload.get("user")
        if isinstance(direct_user, dict):
            raw_user_id = direct_user.get("id")
            if isinstance(raw_user_id, str) and raw_user_id.strip():
                user_id = raw_user_id.strip()
    if user_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing interaction user_id")

    normalized_command = command_name.strip().lower()
    options = data.get("options")
    command_text = f"!{normalized_command}"
    command_params: dict[str, str] | None = None
    attachments: list[dict[str, str]] = []
    if normalized_command == "ask":
        issue_key = _discord_option_value(options, name="issue_key")
        question = _discord_option_value(options, name="question")
        if issue_key:
            command_text = f"{command_text} @{issue_key}"
        if question:
            command_text = f"{command_text} {question}"
    elif normalized_command == "run":
        issue_key = _discord_option_value(options, name="issue_key")
        if issue_key:
            command_text = f"{command_text} {issue_key}"
    elif normalized_command == "link":
        issue_key = _discord_option_value(options, name="issue_key")
        if issue_key:
            command_text = f"{command_text} {issue_key}"
    elif normalized_command == "retry":
        target = _discord_option_value(options, name="target")
        if target:
            command_text = f"{command_text} {target}"
    elif normalized_command == "gap":
        issue_key = _discord_option_value(options, name="issue_key")
        if issue_key:
            command_text = f"{command_text} {issue_key}"
            command_params = {"issue_key": issue_key}
    elif normalized_command == "issues":
        subcommands = options if isinstance(options, list) else []
        for option in subcommands:
            if not isinstance(option, dict):
                continue
            if option.get("type") != 1:
                continue
            subcommand_name = str(option.get("name") or "").strip().lower()
            if not subcommand_name:
                continue
            command_text = f"{command_text} {subcommand_name}"
            spec = _discord_option_value(option.get("options"), name="spec")
            if spec:
                command_text = f"{command_text} {spec}"
            break
    elif normalized_command == "request":
        permission = _discord_option_value(options, name="permission")
        reason = _discord_option_value(options, name="reason")
        if permission:
            command_text = f"{command_text} {permission}"
        if reason:
            command_text = f"{command_text} {reason}"
    elif normalized_command == "bug":
        summary = _discord_option_value(options, name="summary")
        details = _discord_option_value(options, name="details")
        issue_key = _discord_option_value(options, name="issue_key")
        attachment_ids = _discord_option_attachment_ids(options)
        attachments = _discord_resolved_attachments(data=data, attachment_ids=attachment_ids)
        command_params = {}
        if summary:
            command_params["summary"] = summary
            command_text = f"{command_text} {summary}"
        if details:
            command_params["details"] = details
        if issue_key:
            command_params["issue_key"] = issue_key
    else:
        option_values = _flatten_discord_option_values(options)
        if option_values:
            command_text = f"{command_text} {' '.join(option_values)}"

    return user_id, channel_id.strip(), command_text, command_params, attachments


def _parse_ask_confirmation_custom_id(custom_id: str) -> tuple[str, str] | None:
    match = ASK_CONFIRM_CUSTOM_ID_PATTERN.match(custom_id.strip())
    if match is None:
        return None
    return match.group(1), match.group(2)


def _parse_ask_reply_modal_custom_id(custom_id: str) -> str | None:
    match = ASK_REPLY_MODAL_CUSTOM_ID_PATTERN.match(custom_id.strip())
    if match is None:
        return None
    return match.group(1)


def _discord_interaction_modal_response(
    *,
    custom_id: str,
    title: str,
    text_input_custom_id: str,
    text_input_label: str,
    placeholder: str,
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "type": 9,
            "data": {
                "custom_id": custom_id,
                "title": title[:45],
                "components": [
                    {
                        "type": 1,
                        "components": [
                            {
                                "type": 4,
                                "custom_id": text_input_custom_id,
                                "label": text_input_label[:45],
                                "style": 2,
                                "min_length": 1,
                                "max_length": 1800,
                                "required": True,
                                "placeholder": placeholder[:100],
                            }
                        ],
                    }
                ],
            },
        },
    )


def _discord_modal_text_value(payload: dict, *, custom_id: str) -> str | None:
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    rows = data.get("components")
    if not isinstance(rows, list):
        return None
    for row in rows:
        if not isinstance(row, dict):
            continue
        components = row.get("components")
        if not isinstance(components, list):
            continue
        for component in components:
            if not isinstance(component, dict):
                continue
            if component.get("type") != 4:
                continue
            component_custom_id = str(component.get("custom_id") or "").strip()
            if component_custom_id != custom_id:
                continue
            value = component.get("value")
            if value is None:
                return None
            normalized = str(value).strip()
            return normalized or None
    return None


def _ask_confirmation_components(request_id: str) -> list[dict]:
    return build_ask_confirmation_components(request_id)


def _ask_reply_components() -> list[dict]:
    return [
        {
            "type": 1,
            "components": [
                {
                    "type": 2,
                    "style": 1,
                    "label": "Reply",
                    "custom_id": ASK_REPLY_OPEN_CUSTOM_ID,
                }
            ],
        }
    ]


def _validate_webhook_auth(
    tenant: Tenant,
    request: Request,
    request_id: str,
    *,
    session: Session,
    settings,
) -> None:  # noqa: ANN001
    webhook_secret_ref = tenant.jira_config.get("webhook_secret_ref")
    if not webhook_secret_ref:
        return

    expected_token = resolve_scoped_secret_ref(
        session,
        secret_ref=str(webhook_secret_ref),
        encryption_key=settings.secrets_encryption_key,
        tenant_id=tenant.tenant_id,
    )
    if not expected_token:
        logger.error(
            "jira_webhook_auth_misconfigured request_id=%s tenant_id=%s secret_ref=%s",
            request_id,
            tenant.tenant_id,
            webhook_secret_ref,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Webhook authentication is misconfigured",
        )

    presented_token = _extract_webhook_token(request)
    if not presented_token or not secrets.compare_digest(presented_token, expected_token):
        logger.warning(
            "jira_webhook_auth_failed request_id=%s tenant_id=%s",
            request_id,
            tenant.tenant_id,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook token",
        )


def _record_jira_webhook_receipt(
    *,
    session: Session,
    tenant: Tenant,
    delivery_id: str | None,
    issue_key: str,
) -> None:
    jira_config = dict(tenant.jira_config)
    jira_config["webhook_last_received_at"] = datetime.now(timezone.utc).isoformat()
    jira_config["webhook_last_issue_key"] = issue_key
    if delivery_id:
        jira_config["webhook_last_delivery_id"] = delivery_id
    tenant.jira_config = jira_config
    tenant.updated_at = datetime.now(timezone.utc)
    session.commit()


def _resolve_global_github_webhook_secret(
    *,
    request_id: str,
    session: Session,
    settings,
) -> str | None:  # noqa: ANN001
    secret_value = resolve_scoped_secret_ref(
        session,
        secret_ref=GLOBAL_GITHUB_WEBHOOK_SECRET_REF,
        encryption_key=settings.secrets_encryption_key,
    )
    if not secret_value:
        return None
    return secret_value


def _resolve_tenant_github_webhook_secret(
    *,
    tenant: Tenant,
    request_id: str,
    session: Session,
    settings,
) -> str | None:  # noqa: ANN001
    webhook_secret_ref = tenant.github_config.get("webhook_secret_ref")
    if not webhook_secret_ref:
        return None

    secret_value = resolve_scoped_secret_ref(
        session,
        secret_ref=str(webhook_secret_ref),
        encryption_key=settings.secrets_encryption_key,
        tenant_id=tenant.tenant_id,
    )
    if not secret_value:
        logger.error(
            "github_webhook_auth_misconfigured request_id=%s tenant_id=%s secret_ref=%s",
            request_id,
            tenant.tenant_id,
            webhook_secret_ref,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="GitHub webhook authentication is misconfigured",
        )
    return secret_value


def _validate_github_webhook_signature(
    *,
    request: Request,
    payload_bytes: bytes,
    shared_secret: str,
    request_id: str,
    tenant_id: str | None,
) -> None:
    presented_signature = (request.headers.get("X-Hub-Signature-256") or "").strip()
    if not presented_signature.startswith("sha256="):
        logger.warning(
            "github_webhook_auth_failed request_id=%s tenant_id=%s reason=missing_or_invalid_signature_header",
            request_id,
            tenant_id,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid GitHub webhook signature",
        )

    digest = hmac.new(shared_secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
    expected_signature = f"sha256={digest}"
    if not secrets.compare_digest(presented_signature, expected_signature):
        logger.warning(
            "github_webhook_auth_failed request_id=%s tenant_id=%s reason=signature_mismatch",
            request_id,
            tenant_id,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid GitHub webhook signature",
        )


def _extract_installation_id(payload: dict) -> str | None:
    installation = payload.get("installation")
    if isinstance(installation, dict):
        installation_id = installation.get("id")
        if isinstance(installation_id, int):
            return str(installation_id)
        if isinstance(installation_id, str) and installation_id.strip():
            return installation_id.strip()

    installation_id_fallback = payload.get("installation_id")
    if isinstance(installation_id_fallback, int):
        return str(installation_id_fallback)
    if isinstance(installation_id_fallback, str) and installation_id_fallback.strip():
        return installation_id_fallback.strip()
    return None


def _build_command_followup_message(
    *,
    session: Session,
    tenant: Tenant,
    user_id: str,
    command_response,
) -> str:  # noqa: ANN001
    return build_command_followup_message(
        user_id=user_id,
        command_response=command_response,
        jira_browse_base_url=resolve_tenant_jira_browse_base_url(session=session, tenant=tenant),
        issue_key_pattern=ISSUE_KEY_PATTERN,
    )


def _send_discord_interaction_followup(
    *,
    application_id: str,
    interaction_token: str,
    content: str,
    ephemeral: bool = False,
    components: list[dict] | None = None,
    reply_to_message_id: str | None = None,
    channel_id: str | None = None,
) -> None:
    normalized_app_id = application_id.strip()
    normalized_token = interaction_token.strip()
    normalized_content = content.strip()
    if not normalized_app_id or not normalized_token or not normalized_content:
        raise ValueError("Discord interaction follow-up payload is incomplete")
    payload: dict[str, object] = {"content": normalized_content}
    if ephemeral:
        payload["flags"] = 64
    if components:
        payload["components"] = components
    if reply_to_message_id and channel_id:
        payload["message_reference"] = {
            "message_id": reply_to_message_id,
            "channel_id": channel_id,
            "fail_if_not_exists": False,
        }
    request = UrlRequest(
        url=f"https://discord.com/api/v10/webhooks/{normalized_app_id}/{normalized_token}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "MasterBuilderDiscordClient/1.0 (+https://github.com/thedarkcder/master-builder)",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=30):
            return
    except HTTPError as exc:
        error_body = exc.read().decode("utf-8")
        raise RuntimeError(f"Discord follow-up request failed ({exc.code}): {error_body}") from exc


def _send_discord_thread_followup(
    *,
    session: Session,
    settings,
    tenant: Tenant,
    channel_id: str,
    reply_to_message_id: str,
    content: str,
    components: list[dict] | None = None,
) -> None:  # noqa: ANN001
    token_ref = settings.discord_bot_token_secret_ref.strip()
    if not token_ref:
        raise RuntimeError("Discord bot token secret ref is not configured")
    bot_token = resolve_scoped_secret_ref(
        session,
        secret_ref=token_ref,
        encryption_key=settings.secrets_encryption_key,
        tenant_id=tenant.tenant_id,
    )
    if not bot_token:
        raise RuntimeError(f"Discord bot token secret '{token_ref}' is missing")
    client = DiscordApiClient(bot_token=bot_token)
    known_thread_ids = _tenant_discord_channel_ids(
        tenant=tenant,
        project_channel_ids=_project_channel_ids_for_tenant(session=session, tenant_id=tenant.tenant_id),
    )
    if channel_id in known_thread_ids:
        client.post_message(channel_id=channel_id, content=content, components=components)
        return
    thread_name = f"{tenant.tenant_id}-{reply_to_message_id[-6:]}".replace(" ", "-")
    try:
        thread_channel_id = client.ensure_thread_for_message(
            channel_id=channel_id,
            message_id=reply_to_message_id,
            thread_name=thread_name[:100],
        )
        client.post_message(channel_id=thread_channel_id, content=content, components=components)
    except DiscordApiError:
        # If the interaction is already inside a thread, Discord can reject nested thread creation.
        # Fallback to posting directly in the current channel/thread to keep reply flow working.
        client.post_message(channel_id=channel_id, content=content, components=components)


def _send_discord_ask_response_with_thread(
    *,
    session: Session,
    settings,
    tenant: Tenant,
    channel_id: str,
    user_id: str,
    content: str,
) -> None:  # noqa: ANN001
    token_ref = settings.discord_bot_token_secret_ref.strip()
    if not token_ref:
        raise RuntimeError("Discord bot token secret ref is not configured")
    bot_token = resolve_scoped_secret_ref(
        session,
        secret_ref=token_ref,
        encryption_key=settings.secrets_encryption_key,
        tenant_id=tenant.tenant_id,
    )
    if not bot_token:
        raise RuntimeError(f"Discord bot token secret '{token_ref}' is missing")
    client = DiscordApiClient(bot_token=bot_token)
    posted = client.post_message(
        channel_id=channel_id,
        content=content,
        components=_ask_reply_components(),
    )
    posted_message_id = str(posted.get("id") or "").strip()
    if not posted_message_id:
        raise RuntimeError("Discord message post succeeded but response did not include message ID")
    thread_name = f"{tenant.tenant_id}-ask-{posted_message_id[-6:]}".replace(" ", "-")
    thread_channel_id = client.create_thread_from_message(
        channel_id=channel_id,
        message_id=posted_message_id,
        name=thread_name[:100],
    )
    project = _resolve_project_for_channel(session=session, tenant=tenant, channel_id=channel_id)
    if project is not None:
        project_discord_config = dict(project.discord_config or {})
        raw_thread_ids = project_discord_config.get("ask_thread_channel_ids")
        thread_ids = (
            [str(value).strip() for value in raw_thread_ids if str(value).strip()]
            if isinstance(raw_thread_ids, list)
            else []
        )
        if thread_channel_id not in thread_ids:
            thread_ids.append(thread_channel_id)
        project_discord_config["ask_thread_channel_ids"] = thread_ids[-200:]
        project.discord_config = project_discord_config
        project.updated_at = datetime.now(timezone.utc)
    tenant.updated_at = datetime.now(timezone.utc)
    session.commit()
    client.post_message(
        channel_id=thread_channel_id,
        content=f"<@{user_id}> Continue here with follow-up questions.",
    )


def _send_discord_seed_followup_with_thread(
    *,
    session: Session,
    settings,
    tenant: Tenant,
    channel_id: str,
    user_id: str,
    content: str,
    request_id: str,
    questions: list[str],
) -> None:  # noqa: ANN001
    token_ref = settings.discord_bot_token_secret_ref.strip()
    if not token_ref:
        raise RuntimeError("Discord bot token secret ref is not configured")
    bot_token = resolve_scoped_secret_ref(
        session,
        secret_ref=token_ref,
        encryption_key=settings.secrets_encryption_key,
        tenant_id=tenant.tenant_id,
    )
    if not bot_token:
        raise RuntimeError(f"Discord bot token secret '{token_ref}' is missing")

    client = DiscordApiClient(bot_token=bot_token)
    posted = client.post_message(channel_id=channel_id, content=content)
    posted_message_id = str(posted.get("id") or "").strip()
    if not posted_message_id:
        raise RuntimeError("Discord message post succeeded but response did not include message ID")

    thread_name = f"{tenant.tenant_id}-issues-{posted_message_id[-6:]}".replace(" ", "-")
    thread_channel_id = client.create_thread_from_message(
        channel_id=channel_id,
        message_id=posted_message_id,
        name=thread_name[:100],
    )

    discord_config = dict(tenant.discord_config or {})
    project = _resolve_project_for_channel(session=session, tenant=tenant, channel_id=channel_id)
    if project is not None:
        project_discord_config = dict(project.discord_config or {})
        raw_seed_thread_ids = project_discord_config.get("seed_followup_thread_channel_ids")
        seed_thread_ids = (
            [str(value).strip() for value in raw_seed_thread_ids if str(value).strip()]
            if isinstance(raw_seed_thread_ids, list)
            else []
        )
        if thread_channel_id not in seed_thread_ids:
            seed_thread_ids.append(thread_channel_id)
        project_discord_config["seed_followup_thread_channel_ids"] = seed_thread_ids[-200:]
        project.discord_config = project_discord_config
        project.updated_at = datetime.now(timezone.utc)

    raw_seed_followups = discord_config.get("seed_followups")
    if isinstance(raw_seed_followups, list):
        updated_followups: list[dict] = []
        now_iso = datetime.now(timezone.utc).isoformat()
        for item in raw_seed_followups:
            if not isinstance(item, dict):
                continue
            if str(item.get("request_id") or "").strip() != request_id:
                updated_followups.append(item)
                continue
            raw_channel_ids = item.get("channel_ids")
            channel_ids = (
                [str(value).strip() for value in raw_channel_ids if str(value).strip()]
                if isinstance(raw_channel_ids, list)
                else []
            )
            if channel_id not in channel_ids:
                channel_ids.append(channel_id)
            if thread_channel_id not in channel_ids:
                channel_ids.append(thread_channel_id)
            item["channel_ids"] = channel_ids
            item["updated_at"] = now_iso
            updated_followups.append(item)
        discord_config["seed_followups"] = updated_followups

    tenant.discord_config = discord_config
    tenant.updated_at = datetime.now(timezone.utc)
    session.commit()

    numbered_questions = [f"{idx}. {value}" for idx, value in enumerate(questions, start=1) if value.strip()]
    question_block = "\n".join(numbered_questions) if numbered_questions else "No additional questions."
    client.post_message(
        channel_id=thread_channel_id,
        content=(
            f"<@{user_id}> Continue here with details so I can refine and update the seeded tickets.\n"
            f"{question_block}"
        ),
    )


async def _run_discord_command_followup(
    *,
    tenant_id: str,
    user_id: str,
    channel_id: str,
    command_text: str,
    application_id: str,
    interaction_token: str,
    reply_to_message_id: str | None = None,
    command_params: dict[str, str] | None = None,
    attachments: list[dict[str, str]] | None = None,
) -> None:
    service = DiscordWebhookFollowupService(
        session_factory=create_session_factory(),
        settings_factory=get_settings,
        execute_discord_command=execute_discord_command,
        command_request_factory=DiscordCommandRequest,
        build_command_followup_message=_build_command_followup_message,
        ask_confirmation_components=_ask_confirmation_components,
        ask_reply_components=_ask_reply_components,
        reply_transport=DiscordReplyTransport(
            send_interaction_followup=_send_discord_interaction_followup,
            send_thread_reply=_send_discord_thread_followup,
            send_ask_with_thread=_send_discord_ask_response_with_thread,
            send_seed_with_thread=_send_discord_seed_followup_with_thread,
        ),
        consume_pending_ask_action=consume_pending_ask_action,
    )
    await service.run_discord_command_followup(
        tenant_id=tenant_id,
        user_id=user_id,
        channel_id=channel_id,
        command_text=command_text,
        application_id=application_id,
        interaction_token=interaction_token,
        reply_to_message_id=reply_to_message_id,
        command_params=command_params,
        attachments=attachments,
    )


async def _run_discord_ask_confirmation_followup(
    *,
    tenant_id: str,
    user_id: str,
    channel_id: str,
    decision: str,
    request_id: str,
    application_id: str,
    interaction_token: str,
) -> None:
    service = DiscordWebhookFollowupService(
        session_factory=create_session_factory(),
        settings_factory=get_settings,
        execute_discord_command=execute_discord_command,
        command_request_factory=DiscordCommandRequest,
        build_command_followup_message=_build_command_followup_message,
        ask_confirmation_components=_ask_confirmation_components,
        ask_reply_components=_ask_reply_components,
        reply_transport=DiscordReplyTransport(
            send_interaction_followup=_send_discord_interaction_followup,
            send_thread_reply=_send_discord_thread_followup,
            send_ask_with_thread=_send_discord_ask_response_with_thread,
            send_seed_with_thread=_send_discord_seed_followup_with_thread,
        ),
        consume_pending_ask_action=consume_pending_ask_action,
    )
    await service.run_discord_ask_confirmation_followup(
        tenant_id=tenant_id,
        user_id=user_id,
        channel_id=channel_id,
        decision=decision,
        request_id=request_id,
        application_id=application_id,
        interaction_token=interaction_token,
    )


def _find_tenant_by_installation_id(session: Session, installation_id: str) -> Tenant | None:
    tenants = session.execute(select(Tenant)).scalars().all()
    for tenant in tenants:
        configured_installation_id = str(tenant.github_config.get("installation_id") or "").strip()
        if configured_installation_id and configured_installation_id == installation_id:
            return tenant
    return None


def _extract_repository_full_name(payload: dict) -> str | None:
    repository = payload.get("repository")
    if isinstance(repository, dict):
        full_name = repository.get("full_name")
        if isinstance(full_name, str) and full_name.strip():
            return full_name.strip()
    return None


def _extract_pull_request_targets(payload: dict) -> list[tuple[int, bool]]:
    targets: list[tuple[int, bool]] = []
    seen: set[int] = set()

    pull_request = payload.get("pull_request")
    if isinstance(pull_request, dict):
        number = pull_request.get("number")
        body = pull_request.get("body")
        review_summary_present = isinstance(body, str) and bool(body.strip())
        if isinstance(number, int) and number > 0 and number not in seen:
            targets.append((number, review_summary_present))
            seen.add(number)

    for container_key in ("check_suite", "check_run"):
        container = payload.get(container_key)
        if not isinstance(container, dict):
            continue
        pull_requests = container.get("pull_requests")
        if not isinstance(pull_requests, list):
            continue
        for item in pull_requests:
            if not isinstance(item, dict):
                continue
            number = item.get("number")
            if isinstance(number, int) and number > 0 and number not in seen:
                targets.append((number, True))
                seen.add(number)

    return targets


def _resolve_active_project_for_issue(*, session: Session, tenant_id: str, issue_key: str) -> Project | None:
    return find_active_project_for_issue_key(
        session,
        tenant_id=tenant_id,
        issue_key=issue_key,
    )


def _resolve_active_project_for_repo(
    *,
    session: Session,
    tenant_id: str,
    repo_full_name: str,
) -> Project | None:
    return find_active_project_for_repo_full_name(
        session,
        tenant_id=tenant_id,
        repo_full_name=repo_full_name,
    )


def _jira_webhook_response(
    context: JiraWebhookContext,
    *,
    enqueued: bool,
    reason: str | None,
    **extra: object,
) -> dict:
    payload: dict[str, object] = {
        "request_id": context.request_id,
        "tenant_id": context.tenant_id,
        "project_id": context.project.project_id if context.project is not None else None,
        "issue_key": context.issue_key,
        "enqueued": enqueued,
    }
    if reason is not None:
        payload["reason"] = reason
    payload.update(extra)
    return payload


async def _stage_parse_jira_webhook_context(
    *,
    tenant_id: str,
    tenant: Tenant,
    request: Request,
    request_id: str,
    session: Session,
    settings,  # noqa: ANN001
) -> JiraWebhookContext:
    _validate_webhook_auth(
        tenant=tenant,
        request=request,
        request_id=request_id,
        session=session,
        settings=settings,
    )

    payload, _ = await _read_json_payload(request, request_id=request_id, source="jira")
    webhook_event = _normalize_jira_webhook_event(payload.get("webhookEvent"))

    issue_key, _labels, issue_status, issue_status_category_key, issue_summary, issue_description = (
        _extract_issue_payload(payload)
    )
    comment_command, comment_command_argument, comment_command_error = _parse_jira_comment_command(payload)
    delivery_id = _extract_delivery_id(request)
    _record_jira_webhook_receipt(
        session=session,
        tenant=tenant,
        delivery_id=delivery_id,
        issue_key=issue_key,
    )
    logger.info(
        "jira_webhook_issue_parsed request_id=%s tenant_id=%s issue_key=%s delivery_id=%s webhook_event=%s comment_command=%s comment_command_error=%s",
        request_id,
        tenant_id,
        issue_key,
        delivery_id,
        webhook_event,
        comment_command,
        comment_command_error,
    )
    project = _resolve_active_project_for_issue(
        session=session,
        tenant_id=tenant_id,
        issue_key=issue_key,
    )
    return JiraWebhookContext(
        request_id=request_id,
        tenant_id=tenant_id,
        tenant=tenant,
        payload=payload,
        webhook_event=webhook_event,
        issue_key=issue_key,
        issue_status=issue_status,
        issue_status_category_key=issue_status_category_key,
        issue_summary=issue_summary,
        issue_description=issue_description,
        comment_command=comment_command,
        comment_command_argument=comment_command_argument,
        comment_command_error=comment_command_error,
        delivery_id=delivery_id,
        project=project,
    )


def _stage_handle_issue_deleted(
    *,
    context: JiraWebhookContext,
    session: Session,
) -> dict | None:
    if context.webhook_event != "issue_deleted":
        return None

    removed_entries = remove_issue_key_from_tenant_ask_history(
        session=session,
        tenant=context.tenant,
        issue_key=context.issue_key,
    )
    logger.info(
        "jira_webhook_issue_deleted request_id=%s tenant_id=%s issue_key=%s removed_history_entries=%s",
        context.request_id,
        context.tenant_id,
        context.issue_key,
        removed_entries,
    )
    return _jira_webhook_response(
        context,
        enqueued=False,
        reason="issue_deleted",
        removed_history_entries=removed_entries,
        webhook_event=context.webhook_event,
    )


def _stage_handle_invalid_comment_command(
    *,
    context: JiraWebhookContext,
) -> dict | None:
    if not context.comment_command_error:
        return None
    logger.info(
        "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=invalid_comment_command",
        context.request_id,
        context.tenant_id,
        context.issue_key,
    )
    return _jira_webhook_response(
        context,
        enqueued=False,
        reason="invalid_comment_command",
        webhook_event=context.webhook_event,
    )


def _stage_handle_comment_event_memory(
    *,
    context: JiraWebhookContext,
    session: Session,
) -> int:
    if context.webhook_event not in JIRA_COMMENT_EVENTS:
        return 0
    removed_entries = remove_issue_key_from_tenant_ask_history(
        session=session,
        tenant=context.tenant,
        issue_key=context.issue_key,
    )
    logger.info(
        "jira_webhook_comment_event_memory_cleared request_id=%s tenant_id=%s issue_key=%s webhook_event=%s removed_history_entries=%s",
        context.request_id,
        context.tenant_id,
        context.issue_key,
        context.webhook_event,
        removed_entries,
    )
    return removed_entries


def _stage_handle_comment_without_command(
    *,
    context: JiraWebhookContext,
    removed_history_entries: int,
) -> dict | None:
    if context.webhook_event not in JIRA_COMMENT_EVENTS or context.comment_command is not None:
        return None
    logger.info(
        "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=comment_without_command webhook_event=%s",
        context.request_id,
        context.tenant_id,
        context.issue_key,
        context.webhook_event,
    )
    return _jira_webhook_response(
        context,
        enqueued=False,
        reason="comment_without_command",
        webhook_event=context.webhook_event,
        removed_history_entries=removed_history_entries,
    )


def _stage_handle_comment_ask_command(
    *,
    context: JiraWebhookContext,
    session: Session,
    settings,  # noqa: ANN001
) -> dict | None:
    if context.comment_command != "ask":
        return None

    question = (context.comment_command_argument or "").strip()
    if not question:
        return _jira_webhook_response(
            context,
            enqueued=False,
            reason="invalid_comment_command",
        )

    author_account_id = _extract_jira_comment_author_account_id(context.payload) or "jira-user"
    try:
        ask_response = execute_discord_command(
            session=session,
            tenant_id=context.tenant_id,
            payload=DiscordCommandRequest(
                user_id=author_account_id,
                channel_id=None,
                command=f"!ask @{context.issue_key} {question}",
            ),
        )
        response_text = ask_response.message.strip()
        if not response_text:
            response_text = "I processed your question but returned no response text."
    except HTTPException as exc:
        response_text = f"Unable to process `/mb ask`: {exc.detail}"

    posted, post_error = _post_jira_comment(
        session=session,
        tenant=context.tenant,
        issue_key=context.issue_key,
        comment=response_text,
        settings=settings,
    )
    return _jira_webhook_response(
        context,
        enqueued=False,
        reason="comment_command_ask",
        command=context.comment_command,
        question=question,
        comment_posted=posted,
        comment_error=post_error,
        webhook_event=context.webhook_event,
    )


def _resolve_ready_statuses_for_tenant(tenant: Tenant) -> list[str]:
    configured_ready_statuses = tenant.jira_config.get("ready_statuses")
    if isinstance(configured_ready_statuses, list):
        ready_statuses = [str(status).strip() for status in configured_ready_statuses if str(status).strip()]
    else:
        ready_statuses = []
    return ready_statuses or ["Ready for Agent"]


@router.post("/jira/webhook/{tenant_id}")
async def ingest_jira_webhook(
    tenant_id: str,
    request: Request,
    session: Session = Depends(get_session),
) -> dict:
    settings = get_settings()
    request_id = request.headers.get("X-Request-Id") or str(uuid4())
    logger.info("jira_webhook_received request_id=%s tenant_id=%s", request_id, tenant_id)

    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        logger.warning("jira_webhook_unknown_tenant request_id=%s tenant_id=%s", request_id, tenant_id)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown tenant")
    if not tenant.is_enabled:
        logger.info(
            "jira_webhook_ignored request_id=%s tenant_id=%s reason=tenant_disabled",
            request_id,
            tenant_id,
        )
        return {
            "request_id": request_id,
            "tenant_id": tenant_id,
            "enqueued": False,
            "reason": "tenant_disabled",
        }

    context = await _stage_parse_jira_webhook_context(
        tenant_id=tenant_id,
        tenant=tenant,
        request=request,
        request_id=request_id,
        session=session,
        settings=settings,
    )

    deleted_response = _stage_handle_issue_deleted(context=context, session=session)
    if deleted_response is not None:
        return deleted_response

    invalid_comment_response = _stage_handle_invalid_comment_command(context=context)
    if invalid_comment_response is not None:
        return invalid_comment_response

    removed_history_entries = _stage_handle_comment_event_memory(context=context, session=session)
    comment_without_command_response = _stage_handle_comment_without_command(
        context=context,
        removed_history_entries=removed_history_entries,
    )
    if comment_without_command_response is not None:
        return comment_without_command_response

    comment_ask_response = _stage_handle_comment_ask_command(
        context=context,
        session=session,
        settings=settings,
    )
    if comment_ask_response is not None:
        return comment_ask_response

    ready_statuses = _resolve_ready_statuses_for_tenant(tenant)
    if context.issue_status is None:
        logger.info(
            "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=issue_status_missing",
            request_id,
            tenant_id,
            context.issue_key,
        )
        return _jira_webhook_response(
            context,
            enqueued=False,
            reason="issue_status_missing",
            webhook_event=context.webhook_event,
        )

    if context.issue_status_category_key == "done":
        logger.info(
            "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=issue_done issue_status=%s",
            request_id,
            tenant_id,
            context.issue_key,
            context.issue_status,
        )
        return _jira_webhook_response(
            context,
            enqueued=False,
            reason="issue_done",
            issue_status=context.issue_status,
            webhook_event=context.webhook_event,
        )

    normalized_ready_statuses = {status.casefold() for status in ready_statuses}
    if context.issue_status.casefold() not in normalized_ready_statuses:
        logger.info(
            "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=status_not_ready issue_status=%s",
            request_id,
            tenant_id,
            context.issue_key,
            context.issue_status,
        )
        return _jira_webhook_response(
            context,
            enqueued=False,
            reason="status_not_ready",
            issue_status=context.issue_status,
            ready_statuses=ready_statuses,
            guidance=format_discord_ready_gate_guidance(
                issue_key=context.issue_key,
                issue_status=context.issue_status,
                ready_statuses=ready_statuses,
            ),
            webhook_event=context.webhook_event,
        )

    if context.project is None:
        logger.info(
            "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=project_not_mapped",
            request_id,
            tenant_id,
            context.issue_key,
        )
        return _jira_webhook_response(
            context,
            enqueued=False,
            reason="project_not_mapped",
            command=context.comment_command,
            webhook_event=context.webhook_event,
        )

    from_status, to_status = _extract_status_transition(context.payload)
    trigger_reason = "ready_status_recheck"
    if context.comment_command == "run":
        trigger_reason = "comment_command_run"
    elif context.comment_command == "retry":
        trigger_reason = "comment_command_retry"
    elif (
        to_status is not None
        and to_status.casefold() in normalized_ready_statuses
        and from_status is not None
        and from_status.casefold() != to_status.casefold()
    ):
        trigger_reason = "status_transition_to_ready"
    logger.info(
        "jira_webhook_ready_trigger request_id=%s tenant_id=%s issue_key=%s trigger_reason=%s issue_status=%s from_status=%s to_status=%s",
        request_id,
        tenant_id,
        context.issue_key,
        trigger_reason,
        context.issue_status,
        from_status,
        to_status,
    )

    retry_source_run = None
    resolved_issue_description = context.issue_description
    if context.comment_command == "retry":
        retryable_statuses = {RUN_STATUS_FAILED, RUN_STATUS_BLOCKED, RUN_STATUS_CANCELLED}
        retry_source_run = session.execute(
            select(Run)
            .where(
                Run.tenant_id == tenant_id,
                Run.issue_key == context.issue_key,
                Run.status.in_(retryable_statuses),
            )
            .order_by(Run.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if retry_source_run is None:
            logger.info(
                "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=no_retryable_run",
                request_id,
                tenant_id,
                context.issue_key,
            )
            return _jira_webhook_response(
                context,
                enqueued=False,
                reason="no_retryable_run",
                trigger_reason=trigger_reason,
                webhook_event=context.webhook_event,
            )
        resolved_issue_description = retry_source_run.issue_description

    enqueue_result = enqueue_run(
        session,
        tenant_id=tenant_id,
        project_id=context.project.project_id,
        issue_key=context.issue_key,
        issue_summary=context.issue_summary,
        issue_description=resolved_issue_description,
        repo_url=context.project.github_repository,
        delivery_id=context.delivery_id,
        max_concurrent_runs=tenant.policy_config.get("max_concurrent_runs"),
    )
    if not enqueue_result.enqueued:
        logger.info(
            "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=%s run_id=%s",
            request_id,
            tenant_id,
            context.issue_key,
            enqueue_result.reason,
            enqueue_result.run.run_id,
        )
        return _jira_webhook_response(
            context,
            enqueued=False,
            reason=enqueue_result.reason,
            run_id=enqueue_result.run.run_id,
            trigger_reason=trigger_reason,
            command=context.comment_command,
            webhook_event=context.webhook_event,
        )
    logger.info(
        "jira_webhook_enqueued request_id=%s tenant_id=%s issue_key=%s run_id=%s",
        request_id,
        tenant_id,
        context.issue_key,
        enqueue_result.run.run_id,
    )

    return _jira_webhook_response(
        context,
        enqueued=True,
        reason=None,
        run_id=enqueue_result.run.run_id,
        trigger_reason=trigger_reason,
        command=context.comment_command,
        webhook_event=context.webhook_event,
    )
