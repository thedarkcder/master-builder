from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
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

from orchestrator.api.dependencies import get_session
from orchestrator.api.routes_admin import _jira_oauth_client, _refresh_jira_connection_tokens
from orchestrator.api.routes_discord import (
    _project_filter_jql,
    _search_jira_issues_for_tenant,
    consume_pending_ask_action,
    execute_discord_command,
    remove_issue_key_from_tenant_ask_history,
)
from orchestrator.api.schemas import DiscordCommandRequest
from orchestrator.core.config import get_settings
from orchestrator.core.discord_notifications import send_tenant_discord_message
from orchestrator.core.reviewer import ReviewAgentGate
from orchestrator.core.runs import (
    RUN_STATUS_BLOCKED,
    RUN_STATUS_CANCELLED,
    RUN_STATUS_FAILED,
    enqueue_run,
)
from orchestrator.core.secret_manager import resolve_secret_ref
from orchestrator.core.signal_templates import format_discord_ready_gate_guidance
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import JiraOAuthConnection, Run, Tenant
from orchestrator.tools.github_app import GitHubApiError, github_client_from_tenant_config
from orchestrator.tools.discord_api import DiscordApiClient, DiscordApiError
from orchestrator.tools.jira_oauth import JiraOAuthError

router = APIRouter(tags=["jira-webhook"])

logger = logging.getLogger(__name__)
DEFAULT_WEBHOOK_MAX_BODY_BYTES = 1_048_576
HTTP_413_TOO_LARGE = getattr(
    status,
    "HTTP_413_CONTENT_TOO_LARGE",
    status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
)
GLOBAL_GITHUB_WEBHOOK_SECRET_REF = "GITHUB_WEBHOOK_SECRET"
DISCORD_INTERACTIONS_PUBLIC_KEY_SECRET_REF = "DISCORD_INTERACTIONS_PUBLIC_KEY"
SUPPORTED_JIRA_COMMENT_COMMANDS = {"run", "retry", "ask"}
JIRA_COMMENT_EVENTS = {"comment_created", "comment_updated"}
ISSUE_KEY_PATTERN = re.compile(r"\b[A-Z][A-Z0-9_]+-\d+\b")
ASK_CONFIRM_CUSTOM_ID_PATTERN = re.compile(r"^ask\.(approve|reject)\.([0-9a-f]{32})$")
ASK_REPLY_MODAL_CUSTOM_ID_PATTERN = re.compile(r"^ask\.reply\.([0-9]{15,25})$")
ASK_REPLY_OPEN_CUSTOM_ID = "ask.reply.open"


def _max_webhook_body_bytes() -> int:
    raw_value = os.environ.get("ORCHESTRATOR_WEBHOOK_MAX_BODY_BYTES", str(DEFAULT_WEBHOOK_MAX_BODY_BYTES))
    try:
        parsed = int(raw_value)
        if parsed <= 0:
            raise ValueError
    except ValueError:
        logger.warning(
            "invalid_webhook_max_body_bytes value=%s default=%s",
            raw_value,
            DEFAULT_WEBHOOK_MAX_BODY_BYTES,
        )
        return DEFAULT_WEBHOOK_MAX_BODY_BYTES
    return parsed


async def _read_json_payload(
    request: Request,
    *,
    request_id: str,
    source: str,
) -> tuple[dict, bytes]:
    body = await request.body()
    max_bytes = _max_webhook_body_bytes()
    if len(body) > max_bytes:
        logger.warning(
            "%s_webhook_payload_too_large request_id=%s body_bytes=%s max_bytes=%s",
            source,
            request_id,
            len(body),
            max_bytes,
        )
        raise HTTPException(
            status_code=HTTP_413_TOO_LARGE,
            detail="Payload too large",
        )

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid payload") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid payload")
    return payload, body


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


def _extract_webhook_token(request: Request) -> str | None:
    webhook_token = request.headers.get("X-Webhook-Token")
    if webhook_token:
        return webhook_token.strip()

    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        return auth_header[7:].strip()

    return None


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
    raw_public_key = resolve_secret_ref(
        session,
        secret_ref=DISCORD_INTERACTIONS_PUBLIC_KEY_SECRET_REF,
        encryption_key=settings.secrets_encryption_key,
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
    matches: list[Tenant] = []
    for tenant in tenants:
        if channel_id in _tenant_discord_channel_ids(tenant):
            matches.append(tenant)
    if len(matches) != 1:
        return None
    return matches[0]


def _tenant_discord_channel_ids(tenant: Tenant) -> set[str]:
    discord_config = tenant.discord_config or {}
    channel_ids: set[str] = set()
    configured_channel_id = str(discord_config.get("channel_id") or "").strip()
    if configured_channel_id:
        channel_ids.add(configured_channel_id)
    raw_thread_ids = discord_config.get("ask_thread_channel_ids")
    if isinstance(raw_thread_ids, list):
        for value in raw_thread_ids:
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
    current_value: str,
) -> list[dict]:
    project_jql = _project_filter_jql(tenant)
    normalized = current_value.strip().upper()
    if normalized:
        jql = f'{project_jql} AND key ~ "{normalized}*" ORDER BY updated DESC'
    else:
        jql = f"{project_jql} ORDER BY updated DESC"
    issues = _search_jira_issues_for_tenant(
        session=session,
        tenant=tenant,
        jql=jql,
        max_results=25,
    )

    choices: list[dict] = []
    seen_keys: set[str] = set()
    for issue in issues:
        if issue.key in seen_keys:
            continue
        seen_keys.add(issue.key)
        summary = (issue.summary or "").strip()
        display = f"{issue.key} — {summary}" if summary else issue.key
        choices.append({"name": display[:100], "value": issue.key[:100]})
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
    return [
        {
            "type": 1,
            "components": [
                {
                    "type": 2,
                    "style": 3,
                    "label": "Approve",
                    "custom_id": f"ask.approve.{request_id}",
                },
                {
                    "type": 2,
                    "style": 4,
                    "label": "Reject",
                    "custom_id": f"ask.reject.{request_id}",
                },
            ],
        }
    ]


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

    expected_token = resolve_secret_ref(
        session,
        secret_ref=str(webhook_secret_ref),
        encryption_key=settings.secrets_encryption_key,
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
    secret_value = resolve_secret_ref(
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

    secret_value = resolve_secret_ref(
        session,
        secret_ref=str(webhook_secret_ref),
        encryption_key=settings.secrets_encryption_key,
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


def _tenant_jira_browse_base_url(*, session: Session, tenant: Tenant) -> str | None:
    connection_id = str(tenant.jira_config.get("connection_id") or "").strip()
    if not connection_id:
        return None
    connection = session.get(JiraOAuthConnection, connection_id)
    if connection is None:
        return None
    normalized_site_url = str(connection.site_url or "").strip().rstrip("/")
    if not normalized_site_url:
        return None
    return normalized_site_url


def _build_command_followup_message(
    *,
    session: Session,
    tenant: Tenant,
    user_id: str,
    command_response,
) -> str:  # noqa: ANN001
    response_data = command_response.data if isinstance(command_response.data, dict) else {}
    jira_base_url = _tenant_jira_browse_base_url(session=session, tenant=tenant)
    raw_keys = response_data.get("created_issue_keys")
    created_issue_keys = (
        [str(value).strip().upper() for value in raw_keys if str(value).strip()]
        if isinstance(raw_keys, list)
        else []
    )

    def _issue_link(issue_key: str) -> str:
        if not jira_base_url:
            return issue_key
        return f"[{issue_key}]({jira_base_url}/browse/{issue_key})"

    def _linkify_issue_mentions(text: str) -> str:
        if not jira_base_url:
            return text

        def _replace(match: re.Match[str]) -> str:
            issue_key = match.group(0)
            return _issue_link(issue_key)

        return ISSUE_KEY_PATTERN.sub(_replace, text)

    lines: list[str] = [f"<@{user_id}>"]
    command_name = str(command_response.command or "").strip().lower()
    response_message = str(command_response.message or "").strip()
    if command_name == "issues" and created_issue_keys:
        lines[0] = f"{lines[0]} Issue seeding completed."
    elif command_name == "bug" and created_issue_keys:
        lines[0] = f"{lines[0]} Bug logged."
    elif command_name == "link":
        lines[0] = f"{lines[0]} Here are the links."
    elif command_name in {"run", "retry"}:
        lines[0] = f"{lines[0]} Run queued."
    elif response_message:
        if command_name == "ask":
            response_message = _linkify_issue_mentions(response_message)
        lines[0] = f"{lines[0]} {response_message}"

    if command_name == "link":
        jira_url = str(response_data.get("jira_url") or "").strip()
        pr_url = str(response_data.get("pr_url") or "").strip()
        issue_key = str(response_data.get("issue_key") or "").strip().upper()
        if jira_url or pr_url or issue_key:
            lines.append("")
            lines.append("Links:")
            if jira_url:
                issue_label = issue_key or "Issue"
                lines.append(f"- Jira: [{issue_label}]({jira_url})")
            if pr_url:
                lines.append(f"- PR: [Open PR]({pr_url})")
            if issue_key and not jira_url:
                lines.append(f"- Issue: {_issue_link(issue_key)}")

    if command_name in {"run", "retry"}:
        issue_key = str(response_data.get("issue_key") or "").strip().upper()
        run_id = str(response_data.get("run_id") or "").strip()
        if issue_key or run_id:
            lines.append("")
            lines.append("Queued:")
            if issue_key:
                lines.append(f"- Issue: {_issue_link(issue_key)}")
            if run_id:
                lines.append(f"- Run ID: `{run_id}`")

    if command_name == "runs":
        runs = response_data.get("runs")
        if isinstance(runs, list) and runs:
            lines.append("")
            lines.append("Recent runs:")
            for item in runs[:20]:
                if not isinstance(item, dict):
                    continue
                run_id = str(item.get("run_id") or "").strip()
                issue_key = str(item.get("issue_key") or "").strip().upper()
                status_name = str(item.get("status") or "").strip()
                pr_url = str(item.get("pr_url") or "").strip()
                line_parts: list[str] = []
                if run_id:
                    line_parts.append(f"`{run_id}`")
                if status_name:
                    line_parts.append(status_name)
                if issue_key:
                    line_parts.append(_issue_link(issue_key))
                if pr_url:
                    line_parts.append(f"[PR]({pr_url})")
                if line_parts:
                    lines.append(f"- {' | '.join(line_parts)}")

    if command_name == "status":
        active_runs = response_data.get("active_runs")
        if isinstance(active_runs, list) and active_runs:
            lines.append("")
            lines.append("Active runs:")
            for item in active_runs[:20]:
                if not isinstance(item, dict):
                    continue
                run_id = str(item.get("run_id") or "").strip()
                issue_key = str(item.get("issue_key") or "").strip().upper()
                status_name = str(item.get("status") or "").strip()
                line_parts: list[str] = []
                if run_id:
                    line_parts.append(f"`{run_id}`")
                if status_name:
                    line_parts.append(status_name)
                if issue_key:
                    line_parts.append(_issue_link(issue_key))
                if line_parts:
                    lines.append(f"- {' | '.join(line_parts)}")

    if created_issue_keys:
        lines.append("")
        lines.append("Created issues:")
        for issue_key in created_issue_keys[:20]:
            lines.append(f"- {_issue_link(issue_key)}")
        if len(created_issue_keys) > 20:
            lines.append(f"- ...and {len(created_issue_keys) - 20} more")
    content = "\n".join(lines).strip()
    if len(content) <= 1900:
        return content
    return f"{content[:1897]}..."


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
    bot_token = resolve_secret_ref(
        session,
        secret_ref=token_ref,
        encryption_key=settings.secrets_encryption_key,
    )
    if not bot_token:
        raise RuntimeError(f"Discord bot token secret '{token_ref}' is missing")
    client = DiscordApiClient(bot_token=bot_token)
    thread_name = f"{tenant.tenant_id}-{reply_to_message_id[-6:]}".replace(" ", "-")
    thread_channel_id = client.ensure_thread_for_message(
        channel_id=channel_id,
        message_id=reply_to_message_id,
        thread_name=thread_name[:100],
    )
    client.post_message(channel_id=thread_channel_id, content=content, components=components)


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
    bot_token = resolve_secret_ref(
        session,
        secret_ref=token_ref,
        encryption_key=settings.secrets_encryption_key,
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
    discord_config = dict(tenant.discord_config or {})
    raw_thread_ids = discord_config.get("ask_thread_channel_ids")
    thread_ids = (
        [str(value).strip() for value in raw_thread_ids if str(value).strip()]
        if isinstance(raw_thread_ids, list)
        else []
    )
    if thread_channel_id not in thread_ids:
        thread_ids.append(thread_channel_id)
    discord_config["ask_thread_channel_ids"] = thread_ids[-200:]
    tenant.discord_config = discord_config
    tenant.updated_at = datetime.now(timezone.utc)
    session.commit()
    client.post_message(
        channel_id=thread_channel_id,
        content=f"<@{user_id}> Continue here with follow-up questions.",
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
    session_factory = create_session_factory()
    settings = get_settings()
    content = f"<@{user_id}> Command failed due to an internal error."
    components: list[dict] | None = None
    sent_to_thread = False
    try:
        with session_factory() as session:
            tenant = session.get(Tenant, tenant_id)
            if tenant is None or not tenant.is_enabled:
                content = f"<@{user_id}> Command failed: tenant is unavailable."
            else:
                try:
                    command_response = execute_discord_command(
                        tenant_id=tenant_id,
                        payload=DiscordCommandRequest(
                            user_id=user_id,
                            command=command_text,
                            channel_id=channel_id,
                            command_params=command_params,
                            attachments=attachments or [],
                        ),
                        session=session,
                        defer_seed_issues=False,
                        require_ask_confirmation=True,
                    )
                    data = command_response.data if isinstance(command_response.data, dict) else {}
                    requires_confirmation = bool(data.get("requires_confirmation")) and command_response.command == "ask"
                    if requires_confirmation:
                        request_id = str(data.get("request_id") or "").strip()
                        proposed_command = str(data.get("proposed_command") or "").strip()
                        summary = str(data.get("summary") or command_response.message or "").strip()
                        if request_id and proposed_command:
                            lines = [f"<@{user_id}> {summary}", "", f"Proposed action: `{proposed_command}`", "Approve this action?"]
                            content = "\n".join(lines)
                            components = _ask_confirmation_components(request_id)
                        else:
                            content = f"<@{user_id}> Command failed: ask confirmation payload was incomplete."
                    else:
                        content = _build_command_followup_message(
                            session=session,
                            tenant=tenant,
                            user_id=user_id,
                            command_response=command_response,
                        )
                        if command_response.command == "ask" and not reply_to_message_id:
                            try:
                                _send_discord_ask_response_with_thread(
                                    session=session,
                                    settings=settings,
                                    tenant=tenant,
                                    channel_id=channel_id,
                                    user_id=user_id,
                                    content=content,
                                )
                                sent_to_thread = True
                            except (DiscordApiError, RuntimeError, ValueError):
                                logger.exception(
                                    "discord_ask_thread_send_failed tenant_id=%s user_id=%s",
                                    tenant_id,
                                    user_id,
                                )
                                components = _ask_reply_components()
                except HTTPException as exc:
                    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
                    content = f"<@{user_id}> Command failed: {detail}"
                except Exception:  # pragma: no cover - defensive logging path
                    logger.exception(
                        "discord_command_followup_failed tenant_id=%s user_id=%s",
                        tenant_id,
                        user_id,
                    )
                    content = f"<@{user_id}> Command failed due to an internal error."
                if reply_to_message_id and not sent_to_thread:
                    try:
                        _send_discord_thread_followup(
                            session=session,
                            settings=settings,
                            tenant=tenant,
                            channel_id=channel_id,
                            reply_to_message_id=reply_to_message_id,
                            content=content,
                            components=components,
                        )
                        sent_to_thread = True
                    except (DiscordApiError, RuntimeError, ValueError):
                        logger.exception(
                            "discord_thread_followup_send_failed tenant_id=%s user_id=%s message_id=%s",
                            tenant_id,
                            user_id,
                            reply_to_message_id,
                        )
    except Exception:  # pragma: no cover - defensive logging path
        logger.exception(
            "discord_command_followup_runtime_failed tenant_id=%s user_id=%s",
            tenant_id,
            user_id,
        )
    if sent_to_thread:
        return
    try:
        _send_discord_interaction_followup(
            application_id=application_id,
            interaction_token=interaction_token,
            content=content,
            ephemeral=False,
            components=components,
            reply_to_message_id=reply_to_message_id,
            channel_id=channel_id,
        )
    except Exception:  # pragma: no cover - defensive logging path
        logger.exception(
            "discord_command_followup_send_failed tenant_id=%s user_id=%s",
            tenant_id,
            user_id,
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
    session_factory = create_session_factory()
    content = f"<@{user_id}> Failed to process ask confirmation."
    try:
        with session_factory() as session:
            tenant = session.get(Tenant, tenant_id)
            if tenant is None or not tenant.is_enabled:
                content = f"<@{user_id}> Ask confirmation failed: tenant is unavailable."
            else:
                pending = consume_pending_ask_action(
                    session=session,
                    tenant=tenant,
                    request_id=request_id,
                )
                if pending is None:
                    content = f"<@{user_id}> This ask approval request is no longer available."
                else:
                    pending_user_id = str(pending.get("user_id") or "").strip()
                    pending_channel_id = str(pending.get("channel_id") or "").strip()
                    if pending_user_id and pending_user_id != user_id:
                        content = f"<@{user_id}> Only the original requester can approve or reject this action."
                    elif pending_channel_id and pending_channel_id != channel_id:
                        content = f"<@{user_id}> This ask approval is tied to a different channel."
                    elif decision == "reject":
                        content = f"<@{user_id}> Action rejected. No changes were made."
                    else:
                        proposed_command = str(pending.get("proposed_command") or "").strip()
                        if not proposed_command:
                            content = f"<@{user_id}> Ask approval failed: missing proposed command."
                        elif proposed_command.lower().startswith("!ask"):
                            content = f"<@{user_id}> Ask approval failed: recursive ask actions are not allowed."
                        else:
                            try:
                                command_response = execute_discord_command(
                                    tenant_id=tenant_id,
                                    payload=DiscordCommandRequest(
                                        user_id=user_id,
                                        command=proposed_command,
                                        channel_id=channel_id,
                                    ),
                                    session=session,
                                    defer_seed_issues=False,
                                    require_ask_confirmation=False,
                                )
                                content = _build_command_followup_message(
                                    session=session,
                                    tenant=tenant,
                                    user_id=user_id,
                                    command_response=command_response,
                                )
                            except HTTPException as exc:
                                detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
                                content = f"<@{user_id}> Approved action failed: {detail}"
                            except Exception:  # pragma: no cover - defensive path
                                logger.exception(
                                    "discord_ask_approval_execute_failed tenant_id=%s user_id=%s",
                                    tenant_id,
                                    user_id,
                                )
                                content = f"<@{user_id}> Approved action failed due to an internal error."
    except Exception:  # pragma: no cover - defensive logging path
        logger.exception(
            "discord_ask_approval_runtime_failed tenant_id=%s user_id=%s",
            tenant_id,
            user_id,
        )
    try:
        _send_discord_interaction_followup(
            application_id=application_id,
            interaction_token=interaction_token,
            content=content,
            ephemeral=False,
        )
    except Exception:  # pragma: no cover - defensive logging path
        logger.exception(
            "discord_ask_approval_send_failed tenant_id=%s user_id=%s",
            tenant_id,
            user_id,
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

    _validate_webhook_auth(
        tenant=tenant,
        request=request,
        request_id=request_id,
        session=session,
        settings=settings,
    )

    payload, _ = await _read_json_payload(request, request_id=request_id, source="jira")
    raw_webhook_event = payload.get("webhookEvent")
    webhook_event = (
        str(raw_webhook_event).strip().lower()
        if isinstance(raw_webhook_event, str) and str(raw_webhook_event).strip()
        else None
    )

    issue_key, labels, issue_status, issue_status_category_key, issue_summary, issue_description = (
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

    if webhook_event == "jira:issue_deleted":
        removed_entries = remove_issue_key_from_tenant_ask_history(
            session=session,
            tenant=tenant,
            issue_key=issue_key,
        )
        logger.info(
            "jira_webhook_issue_deleted request_id=%s tenant_id=%s issue_key=%s removed_history_entries=%s",
            request_id,
            tenant_id,
            issue_key,
            removed_entries,
        )
        return {
            "request_id": request_id,
            "tenant_id": tenant_id,
            "issue_key": issue_key,
            "enqueued": False,
            "reason": "issue_deleted",
            "removed_history_entries": removed_entries,
            "webhook_event": webhook_event,
        }

    if comment_command_error:
        logger.info(
            "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=invalid_comment_command",
            request_id,
            tenant_id,
            issue_key,
        )
        return {
            "request_id": request_id,
            "tenant_id": tenant_id,
            "issue_key": issue_key,
            "enqueued": False,
            "reason": "invalid_comment_command",
            "webhook_event": webhook_event,
        }

    if webhook_event in JIRA_COMMENT_EVENTS and comment_command is None:
        logger.info(
            "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=comment_without_command webhook_event=%s",
            request_id,
            tenant_id,
            issue_key,
            webhook_event,
        )
        return {
            "request_id": request_id,
            "tenant_id": tenant_id,
            "issue_key": issue_key,
            "enqueued": False,
            "reason": "comment_without_command",
            "webhook_event": webhook_event,
        }

    if comment_command == "ask":
        question = (comment_command_argument or "").strip()
        if not question:
            return {
                "request_id": request_id,
                "tenant_id": tenant_id,
                "issue_key": issue_key,
                "enqueued": False,
                "reason": "invalid_comment_command",
            }
        author_account_id = _extract_jira_comment_author_account_id(payload) or "jira-user"
        try:
            ask_response = execute_discord_command(
                session=session,
                tenant_id=tenant_id,
                payload=DiscordCommandRequest(
                    user_id=author_account_id,
                    channel_id=f"jira:{tenant_id}:{issue_key}",
                    command=f"!ask @{issue_key} {question}",
                ),
            )
            response_text = ask_response.message.strip()
            if not response_text:
                response_text = "I processed your question but returned no response text."
        except HTTPException as exc:
            response_text = f"Unable to process `/mb ask`: {exc.detail}"

        posted, post_error = _post_jira_comment(
            session=session,
            tenant=tenant,
            issue_key=issue_key,
            comment=response_text,
            settings=settings,
        )
        return {
            "request_id": request_id,
            "tenant_id": tenant_id,
            "issue_key": issue_key,
            "enqueued": False,
            "reason": "comment_command_ask",
            "command": comment_command,
            "question": question,
            "comment_posted": posted,
            "comment_error": post_error,
            "webhook_event": webhook_event,
        }

    configured_ready_statuses = tenant.jira_config.get("ready_statuses")
    if isinstance(configured_ready_statuses, list):
        ready_statuses = [str(status).strip() for status in configured_ready_statuses if str(status).strip()]
    else:
        ready_statuses = []
    if not ready_statuses:
        ready_statuses = ["Ready for Agent"]

    if issue_status is None:
        logger.info(
            "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=issue_status_missing",
            request_id,
            tenant_id,
            issue_key,
        )
        return {
            "request_id": request_id,
            "tenant_id": tenant_id,
            "issue_key": issue_key,
            "enqueued": False,
            "reason": "issue_status_missing",
            "webhook_event": webhook_event,
        }

    if issue_status_category_key == "done":
        logger.info(
            "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=issue_done issue_status=%s",
            request_id,
            tenant_id,
            issue_key,
            issue_status,
        )
        return {
            "request_id": request_id,
            "tenant_id": tenant_id,
            "issue_key": issue_key,
            "enqueued": False,
            "reason": "issue_done",
            "issue_status": issue_status,
            "webhook_event": webhook_event,
        }

    normalized_ready_statuses = {status.casefold() for status in ready_statuses}
    if issue_status.casefold() not in normalized_ready_statuses:
        logger.info(
            "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=status_not_ready issue_status=%s",
            request_id,
            tenant_id,
            issue_key,
            issue_status,
        )
        return {
            "request_id": request_id,
            "tenant_id": tenant_id,
            "issue_key": issue_key,
            "enqueued": False,
            "reason": "status_not_ready",
            "issue_status": issue_status,
            "ready_statuses": ready_statuses,
            "guidance": format_discord_ready_gate_guidance(
                issue_key=issue_key,
                issue_status=issue_status,
                ready_statuses=ready_statuses,
            ),
            "webhook_event": webhook_event,
        }

    from_status, to_status = _extract_status_transition(payload)
    trigger_reason = "ready_status_recheck"
    if comment_command == "run":
        trigger_reason = "comment_command_run"
    elif comment_command == "retry":
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
        issue_key,
        trigger_reason,
        issue_status,
        from_status,
        to_status,
    )

    retry_source_run = None
    resolved_issue_description = issue_description
    if comment_command == "retry":
        retryable_statuses = {RUN_STATUS_FAILED, RUN_STATUS_BLOCKED, RUN_STATUS_CANCELLED}
        retry_source_run = session.execute(
            select(Run)
            .where(
                Run.tenant_id == tenant_id,
                Run.issue_key == issue_key,
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
                issue_key,
            )
            return {
                "request_id": request_id,
                "tenant_id": tenant_id,
                "issue_key": issue_key,
                "enqueued": False,
                "reason": "no_retryable_run",
                "trigger_reason": trigger_reason,
                "webhook_event": webhook_event,
            }
        resolved_issue_description = retry_source_run.issue_description

    enqueue_result = enqueue_run(
        session,
        tenant_id=tenant_id,
        issue_key=issue_key,
        issue_summary=issue_summary,
        issue_description=resolved_issue_description,
        delivery_id=delivery_id,
        max_concurrent_runs=tenant.policy_config.get("max_concurrent_runs"),
    )
    if not enqueue_result.enqueued:
        logger.info(
            "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=%s run_id=%s",
            request_id,
            tenant_id,
            issue_key,
            enqueue_result.reason,
            enqueue_result.run.run_id,
        )
        return {
            "request_id": request_id,
            "tenant_id": tenant_id,
            "issue_key": issue_key,
            "enqueued": False,
            "reason": enqueue_result.reason,
            "run_id": enqueue_result.run.run_id,
            "trigger_reason": trigger_reason,
            "command": comment_command,
            "webhook_event": webhook_event,
        }
    logger.info(
        "jira_webhook_enqueued request_id=%s tenant_id=%s issue_key=%s run_id=%s",
        request_id,
        tenant_id,
        issue_key,
        enqueue_result.run.run_id,
    )

    return {
        "request_id": request_id,
        "tenant_id": tenant_id,
        "issue_key": issue_key,
        "enqueued": True,
        "run_id": enqueue_result.run.run_id,
        "trigger_reason": trigger_reason,
        "command": comment_command,
        "webhook_event": webhook_event,
    }


@router.post("/discord/interactions")
async def ingest_discord_interaction(
    request: Request,
    session: Session = Depends(get_session),
) -> JSONResponse:
    settings = get_settings()
    request_id = request.headers.get("X-Request-Id") or str(uuid4())

    payload, payload_bytes = await _read_json_payload(request, request_id=request_id, source="discord")
    public_key = _resolve_discord_interactions_public_key(session=session, settings=settings)
    _validate_discord_interaction_signature(
        request=request,
        payload_bytes=payload_bytes,
        public_key=public_key,
    )

    interaction_type = payload.get("type")
    if interaction_type == 1:  # PING
        return JSONResponse(status_code=status.HTTP_200_OK, content={"type": 1})

    if interaction_type == 4:  # APPLICATION_COMMAND_AUTOCOMPLETE
        channel_id = payload.get("channel_id")
        if not isinstance(channel_id, str) or not channel_id.strip():
            return _discord_autocomplete_response(choices=[])
        tenant = _find_tenant_for_discord_channel(session=session, channel_id=channel_id.strip())
        if tenant is None:
            return _discord_autocomplete_response(choices=[])

        data = payload.get("data")
        if not isinstance(data, dict):
            return _discord_autocomplete_response(choices=[])
        command_name = str(data.get("name") or "").strip().lower()
        focused = _find_focused_discord_option(data.get("options"))
        if focused is None:
            return _discord_autocomplete_response(choices=[])
        focused_name, focused_value = focused
        supports_issue_autocomplete = (
            (command_name in {"run", "link"} and focused_name == "issue_key")
            or (command_name in {"retry"} and focused_name == "target")
            or (command_name in {"ask"} and focused_name == "issue_key")
        )
        if not supports_issue_autocomplete:
            return _discord_autocomplete_response(choices=[])
        try:
            choices = _discord_issue_autocomplete_choices(
                session=session,
                tenant=tenant,
                current_value=focused_value,
            )
        except HTTPException:
            choices = []
        return _discord_autocomplete_response(choices=choices)

    if interaction_type == 3:  # MESSAGE_COMPONENT
        channel_id = payload.get("channel_id")
        if not isinstance(channel_id, str) or not channel_id.strip():
            return _discord_interaction_response(content="Missing interaction channel_id", ephemeral=True)
        tenant = _find_tenant_for_discord_channel(session=session, channel_id=channel_id.strip())
        if tenant is None:
            return _discord_interaction_response(
                content="No enabled tenant is configured for this Discord channel.",
                ephemeral=True,
            )

        application_id = str(payload.get("application_id") or "").strip()
        interaction_token = str(payload.get("token") or "").strip()
        if not application_id or not interaction_token:
            return _discord_interaction_response(
                content="Missing Discord interaction context for deferred response.",
                ephemeral=True,
            )

        component_data = payload.get("data")
        if not isinstance(component_data, dict):
            return _discord_interaction_response(content="Missing component interaction data", ephemeral=True)
        custom_id = str(component_data.get("custom_id") or "").strip()
        if custom_id == ASK_REPLY_OPEN_CUSTOM_ID:
            message = payload.get("message")
            message_id = str(message.get("id") or "").strip() if isinstance(message, dict) else ""
            if not message_id:
                return _discord_interaction_response(
                    content="Unable to open reply form because message context is missing.",
                    ephemeral=True,
                )
            return _discord_interaction_modal_response(
                custom_id=f"ask.reply.{message_id}",
                title="Reply to Master Builder",
                text_input_custom_id="question",
                text_input_label="What should I do next?",
                placeholder="Ask a follow-up question or request the next action.",
            )

        parsed_custom_id = _parse_ask_confirmation_custom_id(custom_id)
        if parsed_custom_id is None:
            return _discord_interaction_response(content="Unsupported interaction action", ephemeral=True)
        decision, request_id = parsed_custom_id

        user_id = None
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
            return _discord_interaction_response(content="Missing interaction user_id", ephemeral=True)

        asyncio.create_task(
            _run_discord_ask_confirmation_followup(
                tenant_id=tenant.tenant_id,
                user_id=user_id,
                channel_id=channel_id.strip(),
                decision=decision,
                request_id=request_id,
                application_id=application_id,
                interaction_token=interaction_token,
            )
        )
        return _discord_interaction_deferred_response(ephemeral=True)

    if interaction_type == 5:  # MODAL_SUBMIT
        channel_id = payload.get("channel_id")
        if not isinstance(channel_id, str) or not channel_id.strip():
            return _discord_interaction_response(content="Missing interaction channel_id", ephemeral=True)
        tenant = _find_tenant_for_discord_channel(session=session, channel_id=channel_id.strip())
        if tenant is None:
            return _discord_interaction_response(
                content="No enabled tenant is configured for this Discord channel.",
                ephemeral=True,
            )

        application_id = str(payload.get("application_id") or "").strip()
        interaction_token = str(payload.get("token") or "").strip()
        if not application_id or not interaction_token:
            return _discord_interaction_response(
                content="Missing Discord interaction context for deferred response.",
                ephemeral=True,
            )

        data = payload.get("data")
        if not isinstance(data, dict):
            return _discord_interaction_response(content="Missing modal interaction data", ephemeral=True)
        reply_to_message_id = _parse_ask_reply_modal_custom_id(str(data.get("custom_id") or "").strip())
        if not reply_to_message_id:
            return _discord_interaction_response(content="Unsupported modal interaction.", ephemeral=True)

        question = _discord_modal_text_value(payload, custom_id="question")
        if not question:
            return _discord_interaction_response(content="Please provide a follow-up question.", ephemeral=True)

        user_id = None
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
            return _discord_interaction_response(content="Missing interaction user_id", ephemeral=True)

        asyncio.create_task(
            _run_discord_command_followup(
                tenant_id=tenant.tenant_id,
                user_id=user_id,
                channel_id=channel_id.strip(),
                command_text=f"!ask {question}",
                application_id=application_id,
                interaction_token=interaction_token,
                reply_to_message_id=reply_to_message_id,
            )
        )
        return _discord_interaction_deferred_response(ephemeral=True)

    if interaction_type != 2:  # APPLICATION_COMMAND
        return _discord_interaction_response(
            content=f"Unsupported Discord interaction type '{interaction_type}'",
            ephemeral=True,
        )

    data = payload.get("data")
    if isinstance(data, dict) and str(data.get("name") or "").strip().lower() == "reply":
        if data.get("type") != 3:
            return _discord_interaction_response(
                content="Reply is a message command. Use it from the message actions menu.",
                ephemeral=True,
            )
        target_message_id = str(data.get("target_id") or "").strip()
        resolved = data.get("resolved")
        resolved_message = None
        if isinstance(resolved, dict):
            resolved_messages = resolved.get("messages")
            if isinstance(resolved_messages, dict):
                resolved_message = resolved_messages.get(target_message_id)
        if not target_message_id:
            return _discord_interaction_response(content="Reply target message was not provided.", ephemeral=True)

        application_id = str(payload.get("application_id") or "").strip()
        if isinstance(resolved_message, dict) and application_id:
            author = resolved_message.get("author")
            author_id = str(author.get("id") or "").strip() if isinstance(author, dict) else ""
            if author_id and author_id != application_id:
                return _discord_interaction_response(
                    content="Use Reply on a Master Builder message.",
                    ephemeral=True,
                )

        return _discord_interaction_modal_response(
            custom_id=f"ask.reply.{target_message_id}",
            title="Reply to Master Builder",
            text_input_custom_id="question",
            text_input_label="What should I do next?",
            placeholder="Ask a follow-up question or request the next action.",
        )

    try:
        user_id, channel_id, command_text, command_params, attachments = _parse_discord_interaction_command(payload)
    except HTTPException as exc:
        return _discord_interaction_response(content=str(exc.detail), ephemeral=True)

    tenant = _find_tenant_for_discord_channel(session=session, channel_id=channel_id)
    if tenant is None:
        return _discord_interaction_response(
            content="No enabled tenant is configured for this Discord channel.",
            ephemeral=True,
        )

    application_id = str(payload.get("application_id") or "").strip()
    interaction_token = str(payload.get("token") or "").strip()
    if not application_id or not interaction_token:
        return _discord_interaction_response(
            content="Missing Discord interaction context for deferred response.",
            ephemeral=True,
        )

    asyncio.create_task(
        _run_discord_command_followup(
            tenant_id=tenant.tenant_id,
            user_id=user_id,
            channel_id=channel_id,
            command_text=command_text,
            application_id=application_id,
            interaction_token=interaction_token,
            command_params=command_params,
            attachments=attachments,
        )
    )
    return _discord_interaction_deferred_response(ephemeral=True)


@router.post("/discord/webhook/{tenant_id}")
async def ingest_discord_webhook(
    tenant_id: str,
    request: Request,
    session: Session = Depends(get_session),
) -> JSONResponse:
    settings = get_settings()
    request_id = request.headers.get("X-Request-Id") or str(uuid4())
    logger.info("discord_webhook_received request_id=%s tenant_id=%s", request_id, tenant_id)

    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown tenant")
    if not tenant.is_enabled:
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "request_id": request_id,
                "tenant_id": tenant_id,
                "accepted": False,
                "reason": "tenant_disabled",
            },
        )

    discord_config = tenant.discord_config or {}
    command_secret_ref = str(discord_config.get("command_secret_ref") or "").strip()
    if command_secret_ref:
        presented_token = _extract_webhook_token(request)
        expected_token = resolve_secret_ref(
            session,
            secret_ref=command_secret_ref,
            encryption_key=settings.secrets_encryption_key,
        )
        if not expected_token:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Discord command authentication is misconfigured",
            )
        if not presented_token or not secrets.compare_digest(presented_token, expected_token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid Discord webhook token",
            )

    payload, _ = await _read_json_payload(request, request_id=request_id, source="discord")
    user_id = payload.get("user_id")
    command = payload.get("command")
    channel_id = payload.get("channel_id")
    if not isinstance(user_id, str) or not user_id.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing user_id")
    if not isinstance(command, str) or not command.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing command")
    if channel_id is not None and not isinstance(channel_id, str):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid channel_id")

    command_response = execute_discord_command(
        tenant_id=tenant_id,
        payload=DiscordCommandRequest(
            user_id=user_id.strip(),
            command=command.strip(),
            channel_id=channel_id.strip() if isinstance(channel_id, str) and channel_id.strip() else None,
        ),
        session=session,
        allow_plain_ask=True,
    )
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "request_id": request_id,
            "tenant_id": tenant_id,
            "accepted": True,
            "result": command_response.model_dump(),
        },
    )


@router.post("/github/webhook")
async def ingest_github_webhook(
    request: Request,
    session: Session = Depends(get_session),
) -> JSONResponse:
    settings = get_settings()
    request_id = request.headers.get("X-Request-Id") or str(uuid4())
    delivery_id = _extract_delivery_id(request) or str(uuid4())
    github_event = (request.headers.get("X-GitHub-Event") or "").strip().lower()

    logger.info(
        "github_webhook_received request_id=%s delivery_id=%s event=%s",
        request_id,
        delivery_id,
        github_event or "unknown",
    )

    payload, payload_bytes = await _read_json_payload(request, request_id=request_id, source="github")
    global_secret = _resolve_global_github_webhook_secret(
        request_id=request_id,
        session=session,
        settings=settings,
    )
    if global_secret is not None:
        _validate_github_webhook_signature(
            request=request,
            payload_bytes=payload_bytes,
            shared_secret=global_secret,
            request_id=request_id,
            tenant_id=None,
        )

    if github_event == "ping":
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "request_id": request_id,
                "delivery_id": delivery_id,
                "event": github_event,
                "accepted": True,
                "reason": "ping",
            },
        )

    installation_id = _extract_installation_id(payload)
    if installation_id is None:
        logger.warning(
            "github_webhook_invalid_payload request_id=%s delivery_id=%s reason=missing_installation_id",
            request_id,
            delivery_id,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing installation identifier",
        )

    tenant = _find_tenant_by_installation_id(session, installation_id=installation_id)
    if tenant is None:
        logger.warning(
            "github_webhook_unknown_installation request_id=%s delivery_id=%s installation_id=%s",
            request_id,
            delivery_id,
            installation_id,
        )
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "request_id": request_id,
                "delivery_id": delivery_id,
                "event": github_event,
                "installation_id": installation_id,
                "accepted": False,
                "reason": "unknown_installation",
            },
        )

    if not tenant.is_enabled:
        logger.info(
            "github_webhook_ignored request_id=%s delivery_id=%s tenant_id=%s reason=tenant_disabled",
            request_id,
            delivery_id,
            tenant.tenant_id,
        )
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "request_id": request_id,
                "delivery_id": delivery_id,
                "tenant_id": tenant.tenant_id,
                "event": github_event,
                "accepted": False,
                "reason": "tenant_disabled",
            },
        )

    if global_secret is None:
        tenant_secret = _resolve_tenant_github_webhook_secret(
            tenant=tenant,
            request_id=request_id,
            session=session,
            settings=settings,
        )
        if tenant_secret is not None:
            _validate_github_webhook_signature(
                request=request,
                payload_bytes=payload_bytes,
                shared_secret=tenant_secret,
                request_id=request_id,
                tenant_id=tenant.tenant_id,
            )

    action = payload.get("action")
    normalized_action = action.strip() if isinstance(action, str) else None
    logger.info(
        "github_webhook_accepted request_id=%s delivery_id=%s tenant_id=%s event=%s action=%s installation_id=%s",
        request_id,
        delivery_id,
        tenant.tenant_id,
        github_event or "unknown",
        normalized_action or "none",
        installation_id,
    )

    review_events = {
        "pull_request",
        "pull_request_review",
        "pull_request_review_comment",
        "check_suite",
        "check_run",
    }
    if github_event not in review_events:
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "request_id": request_id,
                "delivery_id": delivery_id,
                "tenant_id": tenant.tenant_id,
                "event": github_event,
                "action": normalized_action,
                "accepted": True,
                "reason": "ignored_event",
            },
        )

    repo_full_name = _extract_repository_full_name(payload)
    pr_targets = _extract_pull_request_targets(payload)
    if repo_full_name is None or not pr_targets:
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "request_id": request_id,
                "delivery_id": delivery_id,
                "tenant_id": tenant.tenant_id,
                "event": github_event,
                "action": normalized_action,
                "accepted": False,
                "reason": "missing_pr_context",
            },
        )

    def secret_lookup(secret_ref: str) -> str | None:
        return resolve_secret_ref(
            session,
            secret_ref=secret_ref,
            encryption_key=settings.secrets_encryption_key,
        )

    try:
        github_client = github_client_from_tenant_config(
            tenant.github_config,
            secret_lookup=secret_lookup,
        )
        reviewer_gate = ReviewAgentGate(github_client)
    except ValueError as exc:
        logger.warning(
            "github_webhook_review_misconfigured request_id=%s tenant_id=%s error=%s",
            request_id,
            tenant.tenant_id,
            exc,
        )
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "request_id": request_id,
                "delivery_id": delivery_id,
                "tenant_id": tenant.tenant_id,
                "event": github_event,
                "action": normalized_action,
                "accepted": False,
                "reason": "review_misconfigured",
            },
        )

    signals: list[dict[str, object]] = []
    for pr_number, review_summary_present in pr_targets:
        try:
            signal = reviewer_gate.evaluate_pr(
                repo_full_name=repo_full_name,
                pr_number=pr_number,
                review_summary_present=review_summary_present,
            )
        except (GitHubApiError, ValueError) as exc:
            logger.warning(
                "github_webhook_review_failed request_id=%s tenant_id=%s pr_number=%s error=%s",
                request_id,
                tenant.tenant_id,
                pr_number,
                exc,
            )
            return JSONResponse(
                status_code=status.HTTP_202_ACCEPTED,
                content={
                    "request_id": request_id,
                    "delivery_id": delivery_id,
                    "tenant_id": tenant.tenant_id,
                    "event": github_event,
                    "action": normalized_action,
                    "accepted": False,
                    "reason": "review_evaluation_failed",
                },
            )

        send_result = send_tenant_discord_message(
            session=session,
            tenant=tenant,
            message=signal.message,
            settings=settings,
            event="review_signal",
        )
        signals.append(
            {
                "pr_number": pr_number,
                "ready": signal.ready,
                "state": signal.state,
                "policy_pack": signal.policy_pack,
                "must_fix_findings": list(signal.must_fix_findings),
                "discord_sent": send_result.sent,
                "discord_reason": send_result.reason,
            }
        )

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "request_id": request_id,
            "delivery_id": delivery_id,
            "tenant_id": tenant.tenant_id,
            "event": github_event,
            "action": normalized_action,
            "accepted": True,
            "reason": "review_processed",
            "signals": signals,
        },
    )
