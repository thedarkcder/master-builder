from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from uuid import uuid4
from collections.abc import Callable
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select

from orchestrator.api.commands.entrypoint import execute_tenant_discord_command
from orchestrator.api.discord.shared.followup_format import (
    build_ask_confirmation_components,
    build_command_followup_message,
    resolve_tenant_jira_browse_base_url,
)
from orchestrator.api.discord.shared.state import find_seed_followup_context
from orchestrator.api.schemas import DiscordCommandRequest
from orchestrator.core.discord.channel_tenant_index import resolve_tenant_for_discord_channel
from orchestrator.core.config import Settings
from orchestrator.core.error_observability import emit_hard_error
from orchestrator.core.run_human_input_service import (
    pending_human_input_for_thread,
    resume_run_from_human_input_reply,
)
from orchestrator.core.platform_secret_service import (
    PLATFORM_SECRET_DISCORD_BOT_TOKEN_REF,
    resolve_platform_secret_ref,
)
from orchestrator.core.discord.thread_context import get_thread_issue_key
from orchestrator.core.voice import VoiceTranscriptionError, download_audio_bytes, transcribe_audio_bytes
from orchestrator.core.voice.tts import VoiceReplyError, synthesize_reply_audio
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
_ROOM_LIST_KEYS = (
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
_ROOM_SINGLE_KEYS = (
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
_AUDIO_CONTENT_TYPE_PREFIX = "audio/"
_AUDIO_FILENAME_EXTENSIONS = (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".webm", ".flac", ".mp4")


def _ask_reply_components() -> list[dict]:
    return [
        {
            "type": 1,
            "components": [
                {
                    "type": 2,
                    "style": 2,
                    "label": "Reply",
                    "custom_id": "ask.reply.open",
                }
            ],
        }
    ]


def _room_channel_ids_from_discord_config(discord_config: dict | None) -> set[str]:
    config = dict(discord_config or {})
    channel_ids: set[str] = set()
    for key in _ROOM_LIST_KEYS:
        raw_values = config.get(key)
        if not isinstance(raw_values, list):
            continue
        for value in raw_values:
            normalized = str(value or "").strip()
            if normalized:
                channel_ids.add(normalized)
    for key in _ROOM_SINGLE_KEYS:
        normalized = str(config.get(key) or "").strip()
        if normalized:
            channel_ids.add(normalized)
    return channel_ids


def _pm_room_channel_ids_from_discord_config(discord_config: dict | None) -> set[str]:
    return _room_channel_ids_from_discord_config(discord_config)


def _project_room_channel_ids(*, session, tenant_id: str) -> set[str]:  # noqa: ANN001
    projects = session.execute(
        select(Project).where(
            Project.tenant_id == tenant_id,
            Project.is_archived.is_(False),
        )
    ).scalars().all()
    channel_ids: set[str] = set()
    for project in projects:
        channel_ids.update(_room_channel_ids_from_discord_config(project.discord_config or {}))
    return channel_ids


def _project_pm_room_channel_ids(*, session, tenant_id: str) -> set[str]:  # noqa: ANN001
    return _project_room_channel_ids(session=session, tenant_id=tenant_id)


def _is_audio_attachment(attachment: dict[str, str]) -> bool:
    content_type = str(attachment.get("content_type") or "").strip().lower()
    if content_type.startswith(_AUDIO_CONTENT_TYPE_PREFIX):
        return True
    filename = str(attachment.get("filename") or "").strip().lower()
    return any(filename.endswith(extension) for extension in _AUDIO_FILENAME_EXTENSIONS)


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


def _project_seed_followup_thread_project_keys(*, session, tenant_id: str) -> dict[str, str]:  # noqa: ANN001
    projects = session.execute(
        select(Project).where(
            Project.tenant_id == tenant_id,
            Project.is_archived.is_(False),
        )
    ).scalars().all()
    thread_project_keys: dict[str, str] = {}
    for project in projects:
        project_key = str(project.jira_project_key or "").strip().upper()
        if not project_key:
            continue
        raw_seed_thread_ids = (project.discord_config or {}).get("seed_followup_thread_channel_ids")
        if not isinstance(raw_seed_thread_ids, list):
            continue
        for value in raw_seed_thread_ids:
            normalized = str(value or "").strip()
            if normalized:
                thread_project_keys[normalized] = project_key
    return thread_project_keys


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
        issue_key = get_thread_issue_key(discord_config=project.discord_config, channel_id=normalized_channel_id)
        if issue_key and _ISSUE_KEY_PATTERN.fullmatch(issue_key):
            return issue_key
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        return None
    issue_key = get_thread_issue_key(discord_config=tenant.discord_config, channel_id=normalized_channel_id)
    if issue_key and _ISSUE_KEY_PATTERN.fullmatch(issue_key):
        return issue_key
    return None


class DiscordGatewayListener:
    def __init__(
        self,
        *,
        settings: Settings,
        transcribe_audio_attachment: Callable[[dict[str, str]], str] | None = None,
    ) -> None:
        self._settings = settings
        self._session_factory = create_session_factory()
        self._transcribe_audio_attachment = transcribe_audio_attachment
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
        run_loop = self._run_loop()
        try:
            asyncio.run(run_loop)
        except Exception as exc:  # pragma: no cover - defensive
            run_loop.close()
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
        if content.startswith("/"):
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
        should_send_room_voice_reply = False
        voice_note_reply_requested = False
        room_voice_reply_text: str | None = None
        room_voice_reply_persona_id: str | None = None
        room_voice_reply_persona_name: str | None = None
        room_voice_reply_config: dict | None = None
        room_source_mode = "text"

        with self._session_factory() as session:
            tenant = self._find_tenant_for_channel(session=session, channel_id=channel_id)
            if tenant is None:
                return
            room_channel_ids = _project_room_channel_ids(
                session=session,
                tenant_id=tenant.tenant_id,
            )
            room_channel_ids.update(
                _room_channel_ids_from_discord_config(getattr(tenant, "discord_config", None) or {})
            )
            if not content and len(attachments) == 1 and _is_audio_attachment(attachments[0]):
                transcript, error_message = self._transcribe_room_audio_attachment(
                    attachment=attachments[0],
                    bot_token=bot_token,
                )
                if transcript:
                    content = transcript
                    room_source_mode = "voice_note"
                    voice_note_reply_requested = True
                else:
                    graceful_message = error_message or (
                        "I detected an audio attachment but couldn't transcribe it. "
                        "Please send text or configure voice transcription."
                    )
                    try:
                        DiscordApiClient(bot_token=bot_token).post_message(
                            channel_id=channel_id,
                            content=f"<@{user_id}> {graceful_message}",
                        )
                    except DiscordApiError as exc:
                        logger.exception(
                            "discord_gateway_post_failed user_id=%s channel_id=%s error=%s",
                            user_id,
                            channel_id,
                            exc,
                        )
                    return
            if not content:
                return
            pending_human_input = pending_human_input_for_thread(
                session=session,
                tenant_id=tenant.tenant_id,
                thread_channel_id=channel_id,
            )
            if pending_human_input is not None and not content.startswith("!"):
                try:
                    resumed_run = resume_run_from_human_input_reply(
                        session=session,
                        settings=self._settings,
                        request=pending_human_input,
                        reply_text=content,
                        source_ref=str(payload.get("id") or "").strip() or None,
                    )
                    message_content = (
                        f"<@{user_id}> Captured input for `{pending_human_input.issue_key}` "
                        f"and queued resumed run `{resumed_run.run_id}`."
                    )
                except Exception as exc:
                    logger.exception(
                        "discord_gateway_human_input_resume_failed tenant_id=%s user_id=%s channel_id=%s request_id=%s error=%s",
                        tenant.tenant_id,
                        user_id,
                        channel_id,
                        pending_human_input.request_id,
                        exc,
                    )
                    message_content = f"<@{user_id}> Failed to capture the requested input: {exc}"
                try:
                    DiscordApiClient(bot_token=bot_token).post_message(
                        channel_id=channel_id,
                        content=message_content,
                    )
                except DiscordApiError as exc:
                    logger.exception(
                        "discord_gateway_post_failed user_id=%s channel_id=%s error=%s",
                        user_id,
                        channel_id,
                        exc,
                    )
                return
            decision_gate_issue_key = _decision_gate_issue_for_thread(
                session=session,
                tenant_id=tenant.tenant_id,
                channel_id=channel_id,
            )
            if decision_gate_issue_key and not content.startswith("!"):
                command_text = "!reply"
                command_params = {
                    "issue_key": decision_gate_issue_key,
                    "reply_text": content,
                    "source_ref": str(payload.get("id") or "").strip(),
                }
            else:
                command_text = content
                command_params = None
            seed_followup_thread_ids = _project_seed_followup_thread_ids(
                session=session,
                tenant_id=tenant.tenant_id,
            )
            seed_followup_thread_project_keys = _project_seed_followup_thread_project_keys(
                session=session,
                tenant_id=tenant.tenant_id,
            )

            seed_followup_context = find_seed_followup_context(
                tenant=tenant,
                channel_id=channel_id,
            )
            if seed_followup_context is None and channel_id in seed_followup_thread_ids:
                seed_followup_context = find_seed_followup_context(
                    tenant=tenant,
                    channel_id=channel_id,
                    user_id=user_id,
                    project_key=seed_followup_thread_project_keys.get(channel_id),
                )
            if (
                channel_id in seed_followup_thread_ids
                and seed_followup_context is not None
                and not command_text.startswith("!")
            ):
                command_text = f"!issues followup {command_text}"
            if channel_id in room_channel_ids and not command_text.startswith("!"):
                command_text = f"!pm {command_text}"
                command_params = {
                    **(command_params or {}),
                    "room_mode": "true",
                    "room_source": room_source_mode,
                }
            elif voice_note_reply_requested and not command_text.startswith("!"):
                command_text = f"!pm {command_text}"

            message_content = f"<@{user_id}> Command failed due to an internal error."
            components: list[dict] | None = None
            try:
                command_response = execute_tenant_discord_command(
                    tenant_id=tenant.tenant_id,
                    payload=DiscordCommandRequest(
                        user_id=user_id,
                        channel_id=channel_id,
                        command=command_text,
                        command_params=command_params,
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
                elif command_response.command == "reply" and bool(data.get("recheck_required")):
                    components = _ask_reply_components()
                elif (
                    channel_id in room_channel_ids
                    and command_response.command in {"pm", "room"}
                    and self._room_voice_reply_enabled()
                ):
                    should_send_room_voice_reply = True
                    room_voice_reply_text = str(command_response.message or "").strip() or None
                    room_voice_reply_persona_id = str(data.get("persona_id") or "").strip() or None
                    room_voice_reply_persona_name = str(data.get("persona_name") or "").strip() or None
                    if room_voice_reply_persona_id is None and command_response.command == "pm":
                        room_voice_reply_persona_id = "pm"
                    if room_voice_reply_persona_name is None and room_voice_reply_persona_id == "pm":
                        room_voice_reply_persona_name = "PM"
                    room_voice_reply_config = data.get("room_config") if isinstance(data.get("room_config"), dict) else None
                elif (
                    voice_note_reply_requested
                    and command_response.command == "pm"
                    and self._room_voice_reply_enabled()
                ):
                    should_send_room_voice_reply = True
                    room_voice_reply_text = str(command_response.message or "").strip() or None
                    room_voice_reply_persona_id = str(data.get("persona_id") or "").strip() or "pm"
                    room_voice_reply_persona_name = str(data.get("persona_name") or "").strip() or "PM"
                    room_voice_reply_config = data.get("room_config") if isinstance(data.get("room_config"), dict) else None
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
        if should_send_room_voice_reply and room_voice_reply_text:
            voice_error = self._post_room_voice_reply(
                bot_token=bot_token,
                user_id=user_id,
                channel_id=channel_id,
                text=room_voice_reply_text,
                persona_id=room_voice_reply_persona_id,
                persona_name=room_voice_reply_persona_name,
                room_config=room_voice_reply_config,
            )
            if voice_error:
                try:
                    DiscordApiClient(bot_token=bot_token).post_message(
                        channel_id=channel_id,
                        content=f"<@{user_id}> {voice_error}",
                    )
                except DiscordApiError as exc:
                    logger.exception(
                        "discord_gateway_post_failed user_id=%s channel_id=%s error=%s",
                        user_id,
                        channel_id,
                        exc,
                    )

    def _transcribe_room_audio_attachment(
        self,
        *,
        attachment: dict[str, str],
        bot_token: str,
    ) -> tuple[str | None, str | None]:
        if self._transcribe_audio_attachment is None:
            provider = str(getattr(self._settings, "voice_transcription_provider", "disabled") or "").strip().lower()
            if provider in {"", "disabled"}:
                return (
                    None,
                    (
                        "I detected an audio attachment, but voice transcription isn't configured. "
                        "Please share text or enable transcription settings."
                    ),
                )
            try:
                audio_bytes, downloaded_content_type = download_audio_bytes(
                    url=str(attachment.get("url") or ""),
                    bot_token=bot_token,
                )
                transcript = transcribe_audio_bytes(
                    settings=self._settings,
                    audio_bytes=audio_bytes,
                    filename=str(attachment.get("filename") or "").strip() or "voice-note.ogg",
                    content_type=str(attachment.get("content_type") or "").strip() or downloaded_content_type,
                )
            except VoiceTranscriptionError as exc:
                logger.exception("discord_gateway_room_audio_transcription_failed error=%s", exc)
                return None, "I couldn't transcribe that audio attachment. Please retry with text."
            if not transcript:
                return None, "I couldn't transcribe that audio attachment. Please retry with text."
            return transcript, None
        try:
            transcript = str(self._transcribe_audio_attachment(attachment) or "").strip()
        except Exception as exc:
            logger.exception("discord_gateway_room_audio_transcription_failed error=%s", exc)
            return None, "I couldn't transcribe that audio attachment. Please retry with text."
        if not transcript:
            return None, "I couldn't transcribe that audio attachment. Please retry with text."
        return transcript, None

    def _room_voice_reply_enabled(self) -> bool:
        provider = str(getattr(self._settings, "voice_reply_provider", "disabled") or "").strip().lower()
        if provider in {"", "disabled"}:
            return False
        return bool(getattr(self._settings, "voice_reply_enabled_default", False))

    def _post_room_voice_reply(
        self,
        *,
        bot_token: str,
        user_id: str,
        channel_id: str,
        text: str,
        persona_id: str | None = None,
        persona_name: str | None = None,
        room_config: dict | None = None,
    ) -> str | None:
        try:
            audio = synthesize_reply_audio(
                settings=self._settings,
                text=text,
                persona_id=persona_id,
                room_config=room_config,
            )
        except VoiceReplyError as exc:
            logger.warning("discord_gateway_room_voice_reply_failed channel_id=%s reason=%s", channel_id, exc)
            speaker = str(persona_name or persona_id or "Room persona").strip()
            return f"Voice reply failed for `{speaker}`: {exc}"
        try:
            DiscordApiClient(bot_token=bot_token).post_message_with_attachment(
                channel_id=channel_id,
                content=f"<@{user_id}> Voice reply from {str(persona_name or persona_id or 'room persona').strip()}",
                filename=audio.filename,
                file_bytes=audio.audio_bytes,
                content_type=audio.content_type,
            )
        except (DiscordApiError, ValueError) as exc:
            logger.exception(
                "discord_gateway_room_voice_reply_post_failed user_id=%s channel_id=%s error=%s",
                user_id,
                channel_id,
                exc,
            )
            return f"Voice reply post failed: {exc}"
        return None

    def _find_tenant_for_channel(self, *, session, channel_id: str) -> Tenant | None:  # noqa: ANN001
        tenant = resolve_tenant_for_discord_channel(session=session, channel_id=channel_id)
        if tenant is not None:
            return tenant
        projects = session.execute(
            select(Project).where(Project.is_archived.is_(False))
        ).scalars().all()
        matched_tenant_ids = {
            str(project.tenant_id)
            for project in projects
            if channel_id in _room_channel_ids_from_discord_config(project.discord_config or {})
        }
        if len(matched_tenant_ids) != 1:
            return None
        candidate_tenant = session.get(Tenant, next(iter(matched_tenant_ids)))
        if candidate_tenant is None or not candidate_tenant.is_enabled:
            return None
        return candidate_tenant
