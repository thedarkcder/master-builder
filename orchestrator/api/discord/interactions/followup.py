from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.request import Request as UrlRequest, urlopen

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.api.commands.entrypoint import execute_tenant_discord_ingress_command
from orchestrator.api.discord.ask.context import consume_pending_ask_action
from orchestrator.api.discord.interactions.parser import _parse_discord_interaction_command
from orchestrator.api.discord.shared.errors import DiscordInteractionWebhookExpiredError
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
from orchestrator.core.discord.channel_tenant_index import resolve_tenant_for_discord_channel
from orchestrator.core.platform_secret_service import (
    PLATFORM_SECRET_DISCORD_BOT_TOKEN_REF,
    resolve_platform_secret_ref,
)
from orchestrator.core.discord.thread_context import (
    get_thread_issue_key,
    normalize_issue_key,
    put_thread_issue_key,
)
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


def _ask_thread_message_map_from_config(discord_config: dict) -> dict[str, str]:
    raw_map = discord_config.get("ask_thread_by_message_id")
    if not isinstance(raw_map, dict):
        return {}
    normalized: dict[str, str] = {}
    for key, value in raw_map.items():
        message_id = str(key or "").strip()
        thread_id = str(value or "").strip()
        if message_id and thread_id:
            normalized[message_id] = thread_id
    return normalized


def _resolve_thread_id_by_message_suffix(
    *,
    client: DiscordApiClient,
    thread_ids: list[str],
    message_id: str,
) -> str | None:
    suffix = message_id.strip()[-6:]
    if not suffix:
        return None
    for thread_id in thread_ids:
        try:
            channel = client.get_channel(channel_id=thread_id)
        except (DiscordApiError, ValueError) as exc:
            logger.exception(
                "discord_thread_lookup_failed thread_id=%s message_id=%s error=%s",
                thread_id,
                message_id,
                exc,
            )
            continue
        channel_name = str(channel.get("name") or "").strip()
        if channel_name.endswith(suffix):
            return thread_id
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
                    "custom_id": "ask.reply.open",
                }
            ],
        }
    ]


def _decision_gate_issue_for_thread(*, session: Session, channel_id: str) -> tuple[str, str] | None:
    normalized_channel_id = str(channel_id or "").strip()
    if not normalized_channel_id:
        return None
    tenant = resolve_tenant_for_discord_channel(session=session, channel_id=normalized_channel_id)
    if tenant is None:
        return None
    project = resolve_project_for_discord_channel(
        session=session,
        tenant_id=tenant.tenant_id,
        channel_id=normalized_channel_id,
    )
    if project is None:
        return None
    issue_key = get_thread_issue_key(discord_config=project.discord_config, channel_id=normalized_channel_id)
    if not issue_key:
        issue_key = get_thread_issue_key(discord_config=tenant.discord_config, channel_id=normalized_channel_id)
    if not issue_key or ISSUE_KEY_PATTERN.fullmatch(issue_key) is None:
        return None
    return tenant.tenant_id, issue_key


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
        normalized_body = error_body.casefold()
        if exc.code == 404 and ("unknown webhook" in normalized_body or '"code": 10015' in normalized_body):
            raise DiscordInteractionWebhookExpiredError(
                f"Discord interaction follow-up webhook expired ({exc.code}): {error_body}"
            ) from exc
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
    token_ref = PLATFORM_SECRET_DISCORD_BOT_TOKEN_REF
    if not token_ref:
        raise RuntimeError("Discord bot token secret ref is not configured")
    bot_token = resolve_platform_secret_ref(
        session,
        secret_ref=token_ref,
        encryption_key=settings.secrets_encryption_key,
    )
    if not bot_token:
        raise RuntimeError(f"Discord bot token secret '{token_ref}' is missing")
    client = DiscordApiClient(bot_token=bot_token)
    ask_thread_ids = _project_ask_thread_channel_ids_for_tenant(
        session=session,
        tenant_id=tenant.tenant_id,
    )
    seed_thread_ids = _project_seed_followup_thread_channel_ids_for_tenant(
        session=session,
        tenant_id=tenant.tenant_id,
    )
    known_thread_ids = ask_thread_ids | seed_thread_ids
    if channel_id in known_thread_ids:
        client.post_message(channel_id=channel_id, content=content, components=components)
        return
    project = _resolve_project_for_channel(session=session, tenant=tenant, channel_id=channel_id)
    if project is not None:
        project_discord_config = dict(project.discord_config or {})
        ask_message_map = _ask_thread_message_map_from_config(project_discord_config)
        mapped_thread_id = ask_message_map.get(reply_to_message_id)
        if mapped_thread_id:
            try:
                client.post_message(channel_id=mapped_thread_id, content=content, components=components)
                return
            except DiscordApiError as exc:
                logger.exception(
                    "discord_thread_mapped_followup_failed tenant_id=%s channel_id=%s mapped_thread_id=%s reply_to_message_id=%s error=%s",
                    tenant.tenant_id,
                    channel_id,
                    mapped_thread_id,
                    reply_to_message_id,
                    exc,
                )

    thread_name = f"{tenant.tenant_id}-ask-{reply_to_message_id[-6:]}".replace(" ", "-")
    try:
        thread_channel_id = client.ensure_thread_for_message(
            channel_id=channel_id,
            message_id=reply_to_message_id,
            thread_name=thread_name[:100],
        )
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
            ask_message_map = _ask_thread_message_map_from_config(project_discord_config)
            ask_message_map[reply_to_message_id] = thread_channel_id
            project_discord_config["ask_thread_channel_ids"] = thread_ids[-200:]
            project_discord_config["ask_thread_by_message_id"] = dict(list(ask_message_map.items())[-500:])
            project.discord_config = project_discord_config
            project.updated_at = datetime.now(timezone.utc)
            tenant.updated_at = datetime.now(timezone.utc)
            session.commit()
        client.post_message(channel_id=thread_channel_id, content=content, components=components)
    except DiscordApiError as exc:
        logger.exception(
            "discord_thread_followup_failed tenant_id=%s channel_id=%s reply_to_message_id=%s error=%s",
            tenant.tenant_id,
            channel_id,
            reply_to_message_id,
            exc,
        )
        if project is not None:
            project_discord_config = dict(project.discord_config or {})
            raw_thread_ids = project_discord_config.get("ask_thread_channel_ids")
            thread_ids = (
                [str(value).strip() for value in raw_thread_ids if str(value).strip()]
                if isinstance(raw_thread_ids, list)
                else []
            )
            matched_thread_id = _resolve_thread_id_by_message_suffix(
                client=client,
                thread_ids=thread_ids,
                message_id=reply_to_message_id,
            )
            if matched_thread_id:
                ask_message_map = _ask_thread_message_map_from_config(project_discord_config)
                ask_message_map[reply_to_message_id] = matched_thread_id
                project_discord_config["ask_thread_by_message_id"] = dict(list(ask_message_map.items())[-500:])
                project.discord_config = project_discord_config
                project.updated_at = datetime.now(timezone.utc)
                tenant.updated_at = datetime.now(timezone.utc)
                session.commit()
                client.post_message(channel_id=matched_thread_id, content=content, components=components)
                return
        client.post_message(channel_id=channel_id, content=content, components=components)


def _send_discord_ask_response_with_thread(
    *,
    session: Session,
    settings,
    tenant: Tenant,
    channel_id: str,
    user_id: str,
    content: str,
    components: list[dict] | None = None,
    issue_key: str | None = None,
) -> None:  # noqa: ANN001
    token_ref = PLATFORM_SECRET_DISCORD_BOT_TOKEN_REF
    if not token_ref:
        raise RuntimeError("Discord bot token secret ref is not configured")
    bot_token = resolve_platform_secret_ref(
        session,
        secret_ref=token_ref,
        encryption_key=settings.secrets_encryption_key,
    )
    if not bot_token:
        raise RuntimeError(f"Discord bot token secret '{token_ref}' is missing")
    client = DiscordApiClient(bot_token=bot_token)
    ask_thread_channel_ids = _project_ask_thread_channel_ids_for_tenant(
        session=session,
        tenant_id=tenant.tenant_id,
    )
    normalized_issue_key = normalize_issue_key(issue_key)

    def _persist_tenant_thread_issue_binding(*, thread_channel_id: str) -> None:
        if not normalized_issue_key:
            return
        tenant.discord_config = put_thread_issue_key(
            discord_config=tenant.discord_config,
            channel_id=thread_channel_id,
            issue_key=normalized_issue_key,
        )
        tenant.updated_at = datetime.now(timezone.utc)

    def _persist_thread_issue_binding(*, project: Project | None, thread_channel_id: str) -> None:
        if project is None or not normalized_issue_key:
            return
        project.discord_config = put_thread_issue_key(
            discord_config=project.discord_config,
            channel_id=thread_channel_id,
            issue_key=normalized_issue_key,
        )
        project.updated_at = datetime.now(timezone.utc)

    if channel_id in ask_thread_channel_ids:
        project = _resolve_project_for_channel(session=session, tenant=tenant, channel_id=channel_id)
        _persist_thread_issue_binding(project=project, thread_channel_id=channel_id)
        _persist_tenant_thread_issue_binding(thread_channel_id=channel_id)
        if normalized_issue_key:
            session.commit()
        client.post_message(
            channel_id=channel_id,
            content=content,
            components=components or _ask_reply_components(),
        )
        return

    posted = client.post_message(
        channel_id=channel_id,
        content=content,
        components=components or _ask_reply_components(),
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
        ask_message_map = _ask_thread_message_map_from_config(project_discord_config)
        ask_message_map[posted_message_id] = thread_channel_id
        project_discord_config["ask_thread_channel_ids"] = thread_ids[-200:]
        project_discord_config["ask_thread_by_message_id"] = dict(list(ask_message_map.items())[-500:])
        project.discord_config = project_discord_config
        _persist_thread_issue_binding(project=project, thread_channel_id=thread_channel_id)
        project.updated_at = datetime.now(timezone.utc)
    elif normalized_issue_key:
        # Preserve per-thread issue context when a channel is not project-scoped yet.
        logger.info(
            "discord_ask_thread_issue_binding_skipped tenant_id=%s thread_channel_id=%s issue_key=%s reason=project_not_resolved",
            tenant.tenant_id,
            thread_channel_id,
            normalized_issue_key,
        )
    _persist_tenant_thread_issue_binding(thread_channel_id=thread_channel_id)
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
    token_ref = PLATFORM_SECRET_DISCORD_BOT_TOKEN_REF
    if not token_ref:
        raise RuntimeError("Discord bot token secret ref is not configured")
    bot_token = resolve_platform_secret_ref(
        session,
        secret_ref=token_ref,
        encryption_key=settings.secrets_encryption_key,
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
    tenant_id: str | None,
    user_id: str,
    channel_id: str,
    command_text: str,
    application_id: str,
    interaction_token: str,
    reply_to_message_id: str | None = None,
    command_params: dict[str, str] | None = None,
    attachments: list[dict[str, str]] | None = None,
) -> None:
    await asyncio.to_thread(
        _run_discord_command_followup_blocking,
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


async def _run_discord_application_command_followup(
    *,
    payload: dict,
    request_id: str,
) -> None:
    application_id = str(payload.get("application_id") or "").strip()
    interaction_token = str(payload.get("token") or "").strip()
    if not application_id or not interaction_token:
        logger.error(
            "discord_interaction_followup_missing_context request_id=%s",
            request_id,
        )
        return

    try:
        user_id, channel_id, command_text, command_params, attachments = _parse_discord_interaction_command(payload)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        logger.exception(
            "discord_interaction_parse_failed request_id=%s detail=%s error=%s",
            request_id,
            detail,
            exc,
        )
        try:
            _send_discord_interaction_followup(
                application_id=application_id,
                interaction_token=interaction_token,
                content=f"Command failed: {detail}",
                ephemeral=True,
            )
        except Exception as followup_exc:  # pragma: no cover - defensive logging path
            logger.exception(
                "discord_interaction_parse_error_followup_failed request_id=%s error=%s",
                request_id,
                followup_exc,
            )
        return

    await _run_discord_command_followup(
        tenant_id=None,
        user_id=user_id,
        channel_id=channel_id,
        command_text=command_text,
        application_id=application_id,
        interaction_token=interaction_token,
        command_params=command_params,
        attachments=attachments,
    )


async def _run_discord_decision_gate_reply_followup(
    *,
    tenant_id: str | None,
    user_id: str,
    channel_id: str,
    issue_key: str,
    reply_text: str,
    application_id: str,
    interaction_token: str,
    reply_to_message_id: str | None = None,
) -> None:
    await _run_discord_command_followup(
        tenant_id=tenant_id,
        user_id=user_id,
        channel_id=channel_id,
        command_text="!reply",
        application_id=application_id,
        interaction_token=interaction_token,
        reply_to_message_id=reply_to_message_id,
        command_params={
            "issue_key": issue_key,
            "reply_text": reply_text,
        },
    )


def _run_discord_decision_gate_reply_followup_blocking(
    *,
    tenant_id: str | None,
    user_id: str,
    channel_id: str,
    issue_key: str,
    reply_text: str,
    application_id: str,
    interaction_token: str,
    reply_to_message_id: str | None = None,
) -> None:
    _run_discord_command_followup_blocking(
        tenant_id=tenant_id,
        user_id=user_id,
        channel_id=channel_id,
        command_text="!reply",
        application_id=application_id,
        interaction_token=interaction_token,
        reply_to_message_id=reply_to_message_id,
        command_params={
            "issue_key": issue_key,
            "reply_text": reply_text,
        },
    )


def _run_discord_command_followup_blocking(
    *,
    tenant_id: str | None,
    user_id: str,
    channel_id: str,
    command_text: str,
    application_id: str,
    interaction_token: str,
    reply_to_message_id: str | None = None,
    command_params: dict[str, str] | None = None,
    attachments: list[dict[str, str]] | None = None,
) -> None:
    resolved_tenant_id = str(tenant_id or "").strip()
    if not resolved_tenant_id:
        session_factory = create_session_factory()
        with session_factory() as session:
            tenant = resolve_tenant_for_discord_channel(session=session, channel_id=channel_id)
            resolved_tenant_id = tenant.tenant_id if tenant is not None else ""
    if not resolved_tenant_id:
        _send_discord_interaction_followup(
            application_id=application_id,
            interaction_token=interaction_token,
            content="No enabled tenant is configured for this Discord channel.",
            ephemeral=True,
            reply_to_message_id=reply_to_message_id,
            channel_id=channel_id,
        )
        return

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
    asyncio.run(
        service.run_discord_command_followup(
            tenant_id=resolved_tenant_id,
            user_id=user_id,
            channel_id=channel_id,
            command_text=command_text,
            application_id=application_id,
            interaction_token=interaction_token,
            reply_to_message_id=reply_to_message_id,
            command_params=command_params,
            attachments=attachments,
        )
    )


async def _run_discord_ask_confirmation_followup(
    *,
    tenant_id: str | None,
    user_id: str,
    channel_id: str,
    decision: str,
    request_id: str,
    application_id: str,
    interaction_token: str,
) -> None:
    await asyncio.to_thread(
        _run_discord_ask_confirmation_followup_blocking,
        tenant_id=tenant_id,
        user_id=user_id,
        channel_id=channel_id,
        decision=decision,
        request_id=request_id,
        application_id=application_id,
        interaction_token=interaction_token,
    )


def _run_discord_ask_confirmation_followup_blocking(
    *,
    tenant_id: str | None,
    user_id: str,
    channel_id: str,
    decision: str,
    request_id: str,
    application_id: str,
    interaction_token: str,
) -> None:
    resolved_tenant_id = str(tenant_id or "").strip()
    if not resolved_tenant_id:
        session_factory = create_session_factory()
        with session_factory() as session:
            tenant = resolve_tenant_for_discord_channel(session=session, channel_id=channel_id)
            resolved_tenant_id = tenant.tenant_id if tenant is not None else ""
    if not resolved_tenant_id:
        _send_discord_interaction_followup(
            application_id=application_id,
            interaction_token=interaction_token,
            content="No enabled tenant is configured for this Discord channel.",
            ephemeral=True,
        )
        return

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
    asyncio.run(
        service.run_discord_ask_confirmation_followup(
            tenant_id=resolved_tenant_id,
            user_id=user_id,
            channel_id=channel_id,
            decision=decision,
            request_id=request_id,
            application_id=application_id,
            interaction_token=interaction_token,
        )
    )
