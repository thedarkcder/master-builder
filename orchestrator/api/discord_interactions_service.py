from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.request import Request as UrlRequest, urlopen

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.api.command_entrypoint import execute_tenant_discord_ingress_command
from orchestrator.api.discord_ask_context import (
    consume_pending_ask_action,
    project_filter_jql as _project_filter_jql,
    search_jira_issues_for_tenant as _search_jira_issues_for_tenant,
)
from orchestrator.api.discord_followup_format import (
    build_ask_confirmation_components,
    build_command_followup_message,
    resolve_tenant_jira_browse_base_url,
)
from orchestrator.api.discord_reply_transport import DiscordReplyTransport
from orchestrator.api.discord_state_repository import resolve_project_for_discord_channel
from orchestrator.api.schemas import DiscordCommandRequest
from orchestrator.api.webhook_followup_service import DiscordWebhookFollowupService
from orchestrator.core.config import get_settings
from orchestrator.core.discord_channel_tenant_index import resolve_tenant_for_discord_channel
from orchestrator.core.secret_manager import resolve_scoped_secret_ref
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import Project, Tenant
from orchestrator.tools.discord_api import DiscordApiClient, DiscordApiError

logger = logging.getLogger(__name__)

DISCORD_INTERACTIONS_PUBLIC_KEY_SECRET_REF = "DISCORD_INTERACTIONS_PUBLIC_KEY"
ISSUE_KEY_PATTERN = re.compile(r"\b[A-Z][A-Z0-9_]+-\d+\b")
ASK_CONFIRM_CUSTOM_ID_PATTERN = re.compile(r"^ask\.(approve|reject)\.([0-9a-f]{32})$")
ASK_REPLY_MODAL_CUSTOM_ID_PATTERN = re.compile(r"^ask\.reply\.([0-9]{15,25})$")
ASK_REPLY_OPEN_CUSTOM_ID = "ask.reply.open"

execute_discord_ingress_command = execute_tenant_discord_ingress_command

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
    return resolve_tenant_for_discord_channel(session=session, channel_id=channel_id)


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
        execute_command_ingress=execute_discord_ingress_command,
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
        execute_command_ingress=execute_discord_ingress_command,
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

