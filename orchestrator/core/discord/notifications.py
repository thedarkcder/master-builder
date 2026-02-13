from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from orchestrator.core.config import Settings
from orchestrator.core.platform_secret_service import (
    PLATFORM_SECRET_DISCORD_BOT_TOKEN_REF,
    resolve_platform_secret_ref,
)
from orchestrator.storage.models import Project, Tenant
from orchestrator.tools.discord_api import DiscordApiClient, DiscordApiError

logger = logging.getLogger(__name__)
ISSUE_KEY_PATTERN = re.compile(r"\b[A-Z][A-Z0-9_]+-\d+\b")


@dataclass(frozen=True)
class DiscordSendResult:
    sent: bool
    reason: str
    channel_id: str | None = None


def _event_enabled_for_project(*, project: Project | None, event: str | None) -> bool:
    if event is None:
        return True
    normalized = event.strip()
    if not normalized:
        return True
    if project is None:
        return True
    discord_config = project.discord_config or {}
    configured_events = discord_config.get("notify_events")
    if not isinstance(configured_events, list):
        return False
    normalized_events = {str(value).strip() for value in configured_events if str(value).strip()}
    return normalized in normalized_events


def send_tenant_discord_message(
    *,
    session: Session,
    tenant: Tenant,
    project: Project | None = None,
    message: str,
    settings: Settings,
    event: str | None = None,
    open_thread: bool = False,
    thread_name: str | None = None,
    thread_intro: str | None = None,
    thread_intro_components: list[dict] | None = None,
) -> DiscordSendResult:
    if not message.strip():
        return DiscordSendResult(sent=False, reason="empty_message")
    if not _event_enabled_for_project(project=project, event=event):
        return DiscordSendResult(sent=False, reason=f"event_disabled:{event or 'unknown'}")

    project_discord_config = project.discord_config or {} if project is not None else {}
    tenant_discord_config = tenant.discord_config or {}
    channel_id = str(project_discord_config.get("channel_id") or tenant_discord_config.get("channel_id") or "").strip()
    if not channel_id:
        return DiscordSendResult(sent=False, reason="channel_not_configured")

    token_ref = PLATFORM_SECRET_DISCORD_BOT_TOKEN_REF
    if not token_ref:
        return DiscordSendResult(sent=False, reason="bot_token_ref_not_configured")

    bot_token = resolve_platform_secret_ref(
        session,
        secret_ref=token_ref,
        encryption_key=settings.secrets_encryption_key,
    )
    if not bot_token:
        return DiscordSendResult(sent=False, reason=f"bot_token_missing:{token_ref}", channel_id=channel_id)

    try:
        client = DiscordApiClient(bot_token=bot_token)
        posted = client.post_message(channel_id=channel_id, content=message)
        if open_thread:
            posted_message_id = str(posted.get("id") or "").strip()
            if not posted_message_id:
                raise ValueError("Discord message post succeeded but response did not include message ID")
            safe_thread_name = (thread_name or f"{tenant.tenant_id}-update-{posted_message_id[-6:]}").replace(" ", "-")[:100]
            thread_channel_id = client.create_thread_from_message(
                channel_id=channel_id,
                message_id=posted_message_id,
                name=safe_thread_name,
            )
            if project is not None and session is not None:
                # Persist thread/channel mapping so follow-up interactions resolve tenant/project reliably.
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
                if event == "decision_gate_required":
                    matched_issue_key = ISSUE_KEY_PATTERN.search(safe_thread_name)
                    if matched_issue_key is not None:
                        raw_map = project_discord_config.get("decision_gate_thread_issue_by_channel_id")
                        issue_map = dict(raw_map) if isinstance(raw_map, dict) else {}
                        issue_map[thread_channel_id] = matched_issue_key.group(0).upper()
                        project_discord_config["decision_gate_thread_issue_by_channel_id"] = dict(list(issue_map.items())[-500:])
                project.discord_config = project_discord_config
                project.updated_at = datetime.now(timezone.utc)
                tenant.updated_at = datetime.now(timezone.utc)
                session.commit()
            intro = (thread_intro or "").strip()
            if intro:
                client.post_message(
                    channel_id=thread_channel_id,
                    content=intro,
                    components=thread_intro_components,
                )
    except (DiscordApiError, ValueError) as exc:
        logger.warning(
            "discord_message_send_failed tenant_id=%s channel_id=%s error=%s",
            tenant.tenant_id,
            channel_id,
            exc,
        )
        return DiscordSendResult(sent=False, reason=f"send_failed:{exc}", channel_id=channel_id)

    return DiscordSendResult(sent=True, reason="sent", channel_id=channel_id)
