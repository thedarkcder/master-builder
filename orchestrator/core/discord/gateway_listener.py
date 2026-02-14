from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from uuid import uuid4
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select

from orchestrator.api.commands.entrypoint import execute_tenant_discord_command
from orchestrator.api.discord.interactions.followup import _run_discord_decision_gate_reply_followup_blocking
from orchestrator.api.discord.shared.followup_format import (
    build_ask_confirmation_components,
    build_command_followup_message,
    resolve_tenant_jira_browse_base_url,
)
from orchestrator.api.schemas import DiscordCommandRequest
from orchestrator.core.discord.channel_tenant_index import resolve_tenant_for_discord_channel
from orchestrator.core.config import Settings
from orchestrator.core.error_observability import emit_hard_error
from orchestrator.core.platform_secret_service import (
    PLATFORM_SECRET_DISCORD_BOT_TOKEN_REF,
    resolve_platform_secret_ref,
)
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import Project, Tenant
from orchestrator.tools.discord_api import DiscordApiClient, DiscordApiError

try:
    import websockets
except ImportError:  # pragma: no cover - handled at runtime
    websockets = None


logger = logging.getLogger("orchestrator.discord_gateway")

DISCORD_GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"
INTENT_GUILDS = 1 << 0
INTENT_GUILD_MESSAGES = 1 << 9
INTENT_MESSAGE_CONTENT = 1 << 15
_ISSUE_KEY_PATTERN = re.compile(r"\b[A-Z][A-Z0-9_]+-\d+\b")

def _project_seed_followup_thread_ids(*, session, tenant_id: str) -> set[str]:  # noqa: ANN001
    projects = session.execute(
        select(Project).where(
            Project.tenant_id == tenant_id,
            Project.is_archived.is_(False),
        )
    ).scalars().all()
    thread_ids: set[str] = set()
    for project in projects:
        raw_seed_thread_ids = (project.discord_config or {}).get("seed_followup_thread_channel_ids")
        if not isinstance(raw_seed_thread_ids, list):
            continue
        for value in raw_seed_thread_ids:
            normalized = str(value or "").strip()
            if normalized:
                thread_ids.add(normalized)
    return thread_ids


def _decision_gate_issue_for_thread(*, session, tenant_id: str, channel_id: str) -> str | None:  # noqa: ANN001
    normalized_channel_id = str(channel_id or "").strip()
    if not normalized_channel_id:
        return None
    projects = session.execute(
        select(Project).where(
            Project.tenant_id == tenant_id,
            Project.is_archived.is_(False),
        )
    ).scalars().all()
    for project in projects:
        raw_map = (project.discord_config or {}).get("decision_gate_thread_issue_by_channel_id")
        issue_map = raw_map if isinstance(raw_map, dict) else {}
        issue_key = str(issue_map.get(normalized_channel_id) or "").strip().upper()
        if issue_key and _ISSUE_KEY_PATTERN.fullmatch(issue_key):
            return issue_key
    return None


class DiscordGatewayListener:
    def __init__(self, *, settings: Settings) -> None:
        self._settings = settings
        self._session_factory = create_session_factory()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._sequence: int | None = None
        self._resume_gateway_url: str | None = None
        self._session_id: str | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_thread, name="discord-gateway-listener", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def _run_thread(self) -> None:
        try:
            asyncio.run(self._run_loop())
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("discord_gateway_listener_stopped_unexpectedly error=%s", exc)

    async def _run_loop(self) -> None:
        if websockets is None:
            logger.warning("discord_gateway_listener_disabled reason=missing_websockets_dependency")
            return
        token_ref = PLATFORM_SECRET_DISCORD_BOT_TOKEN_REF
        if not token_ref:
            logger.info("discord_gateway_listener_skipped reason=missing_bot_token_ref")
            return

        while not self._stop_event.is_set():
            bot_token = self._resolve_bot_token(token_ref=token_ref)
            if not bot_token:
                logger.info("discord_gateway_listener_waiting_for_bot_token secret_ref=%s", token_ref)
                await asyncio.sleep(5)
                continue
            try:
                await self._run_single_connection(bot_token=bot_token)
            except Exception as exc:
                logger.exception("discord_gateway_listener_connection_failed error=%s", exc)
            if not self._stop_event.is_set():
                await asyncio.sleep(3)

    def _resolve_bot_token(self, *, token_ref: str) -> str:
        with self._session_factory() as session:
            value = resolve_platform_secret_ref(
                session,
                secret_ref=token_ref,
                encryption_key=self._settings.secrets_encryption_key,
            )
        return (value or "").strip()

    async def _run_single_connection(self, *, bot_token: str) -> None:
        gateway_url = self._resume_gateway_url or DISCORD_GATEWAY_URL
        async with websockets.connect(gateway_url, max_size=4 * 1024 * 1024) as websocket:
            hello_payload = json.loads(await websocket.recv())
            heartbeat_interval_ms = int(((hello_payload.get("d") or {}).get("heartbeat_interval") or 45000))
            heartbeat_task = asyncio.create_task(self._heartbeat_loop(websocket, heartbeat_interval_ms / 1000.0))
            try:
                if self._session_id and self._sequence is not None:
                    await websocket.send(
                        json.dumps(
                            {
                                "op": 6,
                                "d": {
                                    "token": bot_token,
                                    "session_id": self._session_id,
                                    "seq": self._sequence,
                                },
                            }
                        )
                    )
                else:
                    await websocket.send(
                        json.dumps(
                            {
                                "op": 2,
                                "d": {
                                    "token": bot_token,
                                    "intents": INTENT_GUILDS | INTENT_GUILD_MESSAGES | INTENT_MESSAGE_CONTENT,
                                    "properties": {
                                        "os": "linux",
                                        "browser": "master-builder",
                                        "device": "master-builder",
                                    },
                                },
                            }
                        )
                    )

                while not self._stop_event.is_set():
                    raw_payload = await websocket.recv()
                    payload = json.loads(raw_payload)
                    sequence = payload.get("s")
                    if isinstance(sequence, int):
                        self._sequence = sequence

                    op = payload.get("op")
                    event_type = str(payload.get("t") or "")
                    data = payload.get("d") if isinstance(payload.get("d"), dict) else {}

                    if op == 7:
                        logger.info("discord_gateway_reconnect_requested")
                        return
                    if op == 9:
                        logger.warning("discord_gateway_invalid_session")
                        self._session_id = None
                        self._sequence = None
                        await asyncio.sleep(2)
                        return
                    if op == 11:
                        continue
                    if op != 0:
                        continue

                    if event_type == "READY":
                        self._session_id = str(data.get("session_id") or "").strip() or None
                        resume_url = str(data.get("resume_gateway_url") or "").strip()
                        self._resume_gateway_url = (
                            f"{resume_url}?v=10&encoding=json" if resume_url else DISCORD_GATEWAY_URL
                        )
                        logger.info("discord_gateway_ready session_id=%s", self._session_id)
                        continue

                    if event_type != "MESSAGE_CREATE":
                        continue

                    await asyncio.to_thread(self._handle_message_create, data, bot_token)
            finally:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    logger.exception("discord_gateway_heartbeat_shutdown_failed error=%s", exc)

    async def _heartbeat_loop(self, websocket: Any, interval_seconds: float) -> None:
        wait_seconds = max(1.0, interval_seconds)
        while not self._stop_event.is_set():
            await asyncio.sleep(wait_seconds)
            await websocket.send(json.dumps({"op": 1, "d": self._sequence}))

    def _handle_message_create(self, payload: dict, bot_token: str) -> None:
        author = payload.get("author")
        if isinstance(author, dict) and author.get("bot") is True:
            return

        channel_id = str(payload.get("channel_id") or "").strip()
        if not channel_id:
            return
        user_id = str((author or {}).get("id") or "").strip()
        if not user_id:
            return
        content = str(payload.get("content") or "").strip()
        if not content or content.startswith("/"):
            return
        raw_attachments = payload.get("attachments")
        attachments: list[dict[str, str]] = []
        if isinstance(raw_attachments, list):
            for item in raw_attachments:
                if not isinstance(item, dict):
                    continue
                url = str(item.get("url") or "").strip()
                if not url:
                    continue
                attachments.append(
                    {
                        "id": str(item.get("id") or "").strip(),
                        "url": url,
                        "filename": str(item.get("filename") or "").strip(),
                        "content_type": str(item.get("content_type") or "").strip(),
                        "size": str(item.get("size") or "").strip(),
                    }
                )
        attachments = attachments[:5]

        with self._session_factory() as session:
            tenant = self._find_tenant_for_channel(session=session, channel_id=channel_id)
            if tenant is None:
                return
            decision_gate_issue_key = _decision_gate_issue_for_thread(
                session=session,
                tenant_id=tenant.tenant_id,
                channel_id=channel_id,
            )
            if decision_gate_issue_key and not content.startswith("!"):
                try:
                    _run_discord_decision_gate_reply_followup_blocking(
                        tenant_id=tenant.tenant_id,
                        user_id=user_id,
                        channel_id=channel_id,
                        issue_key=decision_gate_issue_key,
                        reply_text=content,
                        application_id="discord-gateway",
                        interaction_token="discord-gateway",
                        reply_to_message_id=str(payload.get("id") or "").strip() or None,
                    )
                except Exception as exc:
                    logger.exception(
                        "discord_gateway_decision_gate_reply_failed tenant_id=%s user_id=%s channel_id=%s issue_key=%s error=%s",
                        tenant.tenant_id,
                        user_id,
                        channel_id,
                        decision_gate_issue_key,
                        exc,
                    )
                return
            seed_followup_thread_ids = _project_seed_followup_thread_ids(
                session=session,
                tenant_id=tenant.tenant_id,
            )

            command_text = content
            if channel_id in seed_followup_thread_ids and not command_text.startswith("!"):
                command_text = f"!issues followup {command_text}"

            message_content = f"<@{user_id}> Command failed due to an internal error."
            components: list[dict] | None = None
            try:
                command_response = execute_tenant_discord_command(
                    tenant_id=tenant.tenant_id,
                    payload=DiscordCommandRequest(
                        user_id=user_id,
                        channel_id=channel_id,
                        command=command_text,
                        attachments=attachments,
                    ),
                    session=session,
                    require_ask_confirmation=True,
                    allow_plain_ask=True,
                )
                message_content = build_command_followup_message(
                    user_id=user_id,
                    command_response=command_response,
                    jira_browse_base_url=resolve_tenant_jira_browse_base_url(
                        session=session,
                        tenant=tenant,
                    ),
                    issue_key_pattern=_ISSUE_KEY_PATTERN,
                )
                data = command_response.data if isinstance(command_response.data, dict) else {}
                if (
                    command_response.command == "ask"
                    and bool(data.get("requires_confirmation"))
                    and isinstance(data.get("request_id"), str)
                ):
                    request_id = str(data.get("request_id") or "").strip()
                    if request_id:
                        components = build_ask_confirmation_components(request_id)
            except HTTPException as exc:
                logger.exception(
                    "discord_gateway_command_http_error tenant_id=%s user_id=%s channel_id=%s detail=%s error=%s",
                    tenant.tenant_id,
                    user_id,
                    channel_id,
                    exc.detail,
                    exc,
                )
                message_content = f"<@{user_id}> Command failed: {exc.detail}"
            except Exception as exc:
                error_ref = uuid4().hex[:8]
                logger.exception(
                    "discord_gateway_command_failed tenant_id=%s user_id=%s channel_id=%s error_ref=%s error=%s",
                    tenant.tenant_id,
                    user_id,
                    channel_id,
                    error_ref,
                    exc,
                )
                emit_hard_error(
                    event="discord_gateway_command_failed",
                    error_ref=error_ref,
                    exc=exc,
                    context={
                        "tenant_id": tenant.tenant_id,
                        "user_id": user_id,
                        "channel_id": channel_id,
                    },
                )
                message_content = f"<@{user_id}> Command failed due to an internal error. Ref: `{error_ref}`"

        try:
            DiscordApiClient(bot_token=bot_token).post_message(
                channel_id=channel_id,
                content=message_content,
                components=components,
            )
        except DiscordApiError as exc:
            logger.exception(
                "discord_gateway_post_failed user_id=%s channel_id=%s error=%s",
                user_id,
                channel_id,
                exc,
            )

    def _find_tenant_for_channel(self, *, session, channel_id: str) -> Tenant | None:  # noqa: ANN001
        return resolve_tenant_for_discord_channel(session=session, channel_id=channel_id)
