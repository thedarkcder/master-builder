from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.request import Request as UrlRequest, urlopen

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.api.commands.entrypoint import execute_tenant_discord_ingress_command
from orchestrator.api.discord.ask.context import consume_pending_ask_action
from orchestrator.api.discord.shared.followup_format import (
    build_ask_confirmation_components,
    build_command_followup_message,
    resolve_tenant_jira_browse_base_url,
)
from orchestrator.api.discord.shared.reply_transport import DiscordReplyTransport
from orchestrator.api.discord.shared.state_repository import resolve_project_for_discord_channel
from orchestrator.api.schemas import DiscordCommandRequest
from orchestrator.api.webhooks.followup_service import DiscordWebhookFollowupService
from orchestrator.core.config import get_settings
from orchestrator.core.secret_manager import resolve_scoped_secret_ref
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import Project, Tenant
from orchestrator.tools.discord_api import DiscordApiClient, DiscordApiError

logger = logging.getLogger(__name__)
ISSUE_KEY_PATTERN = re.compile(r"\b[A-Z][A-Z0-9_]+-\d+\b")

execute_discord_ingress_command = execute_tenant_discord_ingress_command


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


def _project_ask_thread_channel_ids_for_tenant(*, session: Session, tenant_id: str) -> set[str]:
    projects = session.execute(
        select(Project).where(
            Project.tenant_id == tenant_id,
            Project.is_archived.is_(False),
        )
    ).scalars().all()
    channel_ids: set[str] = set()
    for project in projects:
        raw_thread_ids = (project.discord_config or {}).get("ask_thread_channel_ids")
        if not isinstance(raw_thread_ids, list):
            continue
        for value in raw_thread_ids:
            normalized = str(value or "").strip()
            if normalized:
                channel_ids.add(normalized)
    return channel_ids


def _project_seed_followup_thread_channel_ids_for_tenant(*, session: Session, tenant_id: str) -> set[str]:
    projects = session.execute(
        select(Project).where(
            Project.tenant_id == tenant_id,
            Project.is_archived.is_(False),
        )
    ).scalars().all()
    channel_ids: set[str] = set()
    for project in projects:
        raw_thread_ids = (project.discord_config or {}).get("seed_followup_thread_channel_ids")
        if not isinstance(raw_thread_ids, list):
            continue
        for value in raw_thread_ids:
            normalized = str(value or "").strip()
            if normalized:
                channel_ids.add(normalized)
    return channel_ids


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
                    "custom_id": "ask.reply.open",
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
    ask_thread_channel_ids = _project_ask_thread_channel_ids_for_tenant(
        session=session,
        tenant_id=tenant.tenant_id,
    )
    if channel_id in ask_thread_channel_ids:
        client.post_message(
            channel_id=channel_id,
            content=content,
            components=_ask_reply_components(),
        )
        return

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
    seed_thread_channel_ids = _project_seed_followup_thread_channel_ids_for_tenant(
        session=session,
        tenant_id=tenant.tenant_id,
    )
    if channel_id in seed_thread_channel_ids:
        numbered_questions = [f"{idx}. {value}" for idx, value in enumerate(questions, start=1) if value.strip()]
        question_block = "\n".join(numbered_questions) if numbered_questions else "No additional questions."
        client.post_message(
            channel_id=channel_id,
            content=(
                f"{content}\n\n"
                f"<@{user_id}> Continue here with details so I can refine and update the seeded tickets.\n"
                f"{question_block}"
            ),
        )
        return

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
