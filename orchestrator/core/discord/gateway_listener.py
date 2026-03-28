from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from datetime import UTC, datetime
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from orchestrator.api.commands.entrypoint import execute_tenant_discord_command
from orchestrator.api.transport_runtime import (
    build_discord_transport_executor,
    build_transport_action_executors,
    execute_side_effect_ingress_result,
)
from orchestrator.api.discord.interactions.application import (
    build_discord_interaction_ingress_result,
)
from orchestrator.api.discord.messages.application import (
    DiscordMessageIngressDeps,
    build_discord_message_ingress_result,
)
from orchestrator.api.discord.interactions.auth import (
    ASK_REPLY_OPEN_CUSTOM_ID,
    _discord_autocomplete_response,
    _discord_interaction_deferred_response,
    _discord_interaction_modal_response,
    _discord_interaction_response,
    _discord_modal_text_value,
    _parse_ask_confirmation_custom_id,
    _parse_ask_reply_modal_custom_id,
)
from orchestrator.api.discord.interactions.dispatcher import DiscordInteractionDispatchDeps
from orchestrator.api.discord.interactions.followup import (
    _resolve_followup_context as _interaction_resolve_followup_context,
    _resolve_followup_reaction as _interaction_resolve_followup_reaction,
    _resolve_thread_channel_for_reply as _interaction_resolve_thread_channel_for_reply,
    _run_discord_application_command_followup,
    _run_discord_ask_confirmation_followup,
    _run_discord_command_followup,
    _run_discord_decision_gate_reply_followup,
)
from orchestrator.api.discord.interactions.followup_transport import (
    send_discord_interaction_callback,
)
from orchestrator.api.discord.interactions.parser import (
    _discord_issue_autocomplete_choices,
    _find_focused_discord_option,
    _find_tenant_for_discord_channel,
)
from orchestrator.api.discord.shared.followup_format import (
    build_ask_confirmation_components,
    build_command_followup_message,
    resolve_tenant_jira_browse_base_url,
)
from orchestrator.api.discord.shared.state import (
    live_voice_linked_channel_ids_from_discord_config,
)
from orchestrator.core.discord.channel_tenant_index import resolve_tenant_for_discord_channel
from orchestrator.core.communications import (
    DiscordChannelMessageWithAttachmentAction,
    IngressResult,
    TransportAction,
    TransportEnvelope,
)
from orchestrator.core.config import Settings
from orchestrator.core.observability import scoped_log_context
from orchestrator.core.discord.personas import (
    build_voice_room_spoken_reply_text,
    format_voice_room_persona_label,
)
from orchestrator.core.error_observability import emit_hard_error
from orchestrator.core.followup_context_service import resolve_followup_context, resolve_followup_reaction
from orchestrator.core.run_human_input_service import pending_human_input_for_request_id, resume_run_from_human_input_reply
from orchestrator.core.platform_secret_service import (
    PLATFORM_SECRET_DISCORD_BOT_TOKEN_REF,
    resolve_platform_secret_ref,
)
from orchestrator.core.voice import VoiceTranscriptionError, download_audio_bytes, transcribe_audio_bytes
from orchestrator.core.voice.tts import VoiceReplyError, synthesize_reply_audio
from orchestrator.api.discord.shared.state_repository import resolve_project_for_discord_channel
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import Project, Tenant, TenantMembership, TenantUserDiscordIdentity
from orchestrator.tools.discord_api import DiscordApiClient, DiscordApiError

try:
    import websockets
except ImportError:  # pragma: no cover - handled at runtime
    websockets = None


logger = logging.getLogger("orchestrator.discord_gateway")

DISCORD_GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"
INTENT_GUILDS = 1 << 0
INTENT_GUILD_MEMBERS = 1 << 1
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
_VOICE_MESSAGE_FLAG = 1 << 13


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
    channel_ids.update(live_voice_linked_channel_ids_from_discord_config(config))
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
    context = resolve_followup_context(
        session=session,
        tenant_id=tenant_id,
        channel_id=channel_id,
    )
    if context is None or str(getattr(context, "context_type", "") or "").strip() != "decision_gate":
        return None
    issue_key = str(getattr(context, "issue_key", "") or "").strip().upper()
    if not issue_key or _ISSUE_KEY_PATTERN.fullmatch(issue_key) is None:
        return None
    return issue_key


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
                                    "intents": INTENT_GUILDS | INTENT_GUILD_MEMBERS | INTENT_GUILD_MESSAGES | INTENT_MESSAGE_CONTENT,
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

                    if event_type == "INTERACTION_CREATE":
                        await self._handle_interaction_create(data)
                        continue

                    if event_type == "MESSAGE_CREATE":
                        await asyncio.to_thread(self._handle_message_create, data, bot_token)
                        continue

                    if event_type == "GUILD_MEMBER_ADD":
                        await asyncio.to_thread(self._handle_guild_member_add, data, bot_token)
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

    def _interaction_dispatch_deps(self) -> DiscordInteractionDispatchDeps:
        return DiscordInteractionDispatchDeps(
            transport_source="discord_gateway",
            ask_reply_open_custom_id=ASK_REPLY_OPEN_CUSTOM_ID,
            autocomplete_response=_discord_autocomplete_response,
            interaction_response=_discord_interaction_response,
            interaction_modal_response=_discord_interaction_modal_response,
            interaction_deferred_response=_discord_interaction_deferred_response,
            parse_ask_confirmation_custom_id=_parse_ask_confirmation_custom_id,
            parse_ask_reply_modal_custom_id=_parse_ask_reply_modal_custom_id,
            discord_modal_text_value=_discord_modal_text_value,
            find_tenant_for_discord_channel=_find_tenant_for_discord_channel,
            find_focused_discord_option=_find_focused_discord_option,
            discord_issue_autocomplete_choices=_discord_issue_autocomplete_choices,
            resolve_thread_channel_for_reply=_interaction_resolve_thread_channel_for_reply,
            resolve_followup_context=_interaction_resolve_followup_context,
            resolve_followup_reaction=_interaction_resolve_followup_reaction,
            run_discord_ask_confirmation_followup=_run_discord_ask_confirmation_followup,
            run_discord_command_followup=_run_discord_command_followup,
            run_discord_decision_gate_reply_followup=_run_discord_decision_gate_reply_followup,
            run_discord_application_command_followup=_run_discord_application_command_followup,
            task_scheduler=asyncio.create_task,
            logger=logger,
        )

    def _message_dispatch_deps(self, *, bot_token: str) -> DiscordMessageIngressDeps:
        return DiscordMessageIngressDeps(
            find_tenant_for_channel=self._find_tenant_for_channel,
            resolve_project_for_discord_channel=resolve_project_for_discord_channel,
            project_room_channel_ids=_project_room_channel_ids,
            room_channel_ids_from_discord_config=_room_channel_ids_from_discord_config,
            is_audio_attachment=_is_audio_attachment,
            transcribe_audio_attachment=lambda **kwargs: self._transcribe_room_audio_attachment(
                bot_token=bot_token,
                **kwargs,
            ),
            load_pending_human_input_request=pending_human_input_for_request_id,
            resume_run_from_human_input_reply=resume_run_from_human_input_reply,
            resolve_followup_context=resolve_followup_context,
            resolve_followup_reaction=resolve_followup_reaction,
            execute_tenant_discord_command=execute_tenant_discord_command,
            resolve_tenant_jira_browse_base_url=resolve_tenant_jira_browse_base_url,
            build_command_followup_message=build_command_followup_message,
            build_ask_confirmation_components=build_ask_confirmation_components,
            ask_reply_components=_ask_reply_components,
            issue_key_pattern=_ISSUE_KEY_PATTERN,
            room_voice_reply_enabled=self._room_voice_reply_enabled,
            build_room_voice_reply_action=self._build_room_voice_reply_action,
            emit_hard_error=emit_hard_error,
            logger=logger,
            settings=self._settings,
        )

    def _execute_ingress_result(
        self,
        *,
        result: IngressResult,
        envelope: TransportEnvelope,
        bot_token: str | None = None,
    ) -> None:
        execute_side_effect_ingress_result(
            result=result,
            envelope=envelope,
            transport_action_executors=self._transport_action_executors(bot_token=bot_token),
            action_error_handler=lambda action, exc: self._handle_ingress_action_error(
                action=action,
                exc=exc,
                bot_token=bot_token,
            ),
        )

    def _transport_action_executors(self, *, bot_token: str | None = None):
        return build_transport_action_executors(
            discord_transport_executor=build_discord_transport_executor(
                bot_token=bot_token,
                interaction_callback_sender=send_discord_interaction_callback,
                client_factory=DiscordApiClient,
            ),
        )

    def _handle_ingress_action_error(
        self,
        *,
        action: TransportAction,
        exc: Exception,
        bot_token: str | None,
    ) -> bool:
        if isinstance(exc, RuntimeError):
            logger.warning(
                "discord_gateway_unsupported_transport_action kind=%s error=%s",
                getattr(action, "kind", type(action).__name__),
                exc,
            )
            return True
        if isinstance(exc, DiscordApiError):
            channel_id = getattr(action, "channel_id", None)
            logger.exception("discord_gateway_post_failed channel_id=%s error=%s", channel_id, exc)
            if isinstance(action, DiscordChannelMessageWithAttachmentAction) and action.failure_user_id and bot_token:
                try:
                    DiscordApiClient(bot_token=bot_token).post_message(
                        channel_id=action.channel_id,
                        content=f"<@{action.failure_user_id}> Voice reply post failed: {exc}",
                    )
                except DiscordApiError as fallback_exc:
                    logger.exception(
                        "discord_gateway_post_failed channel_id=%s error=%s",
                        action.channel_id,
                        fallback_exc,
                    )
            return True
        return False

    async def _handle_interaction_create(self, payload: dict) -> None:
        interaction_id = str(payload.get("id") or "").strip()
        interaction_token = str(payload.get("token") or "").strip()
        if not interaction_id or not interaction_token:
            logger.warning(
                "discord_gateway_interaction_ignored reason=missing_context interaction_id=%s",
                interaction_id,
            )
            return

        request_id = str(uuid4())
        try:
            with self._session_factory() as session:
                result = await build_discord_interaction_ingress_result(
                    payload=payload,
                    session=session,
                    envelope=TransportEnvelope(
                        transport="discord_gateway",
                        event_type="interaction_create",
                        request_id=request_id,
                        payload=payload,
                    ),
                    dispatch_deps=self._interaction_dispatch_deps(),
                )
            self._execute_ingress_result(
                result=result,
                envelope=TransportEnvelope(
                    transport="discord_gateway",
                    event_type="interaction_create",
                    request_id=request_id,
                    payload=payload,
                ),
            )
        except Exception as exc:
            logger.exception(
                "discord_gateway_interaction_failed request_id=%s interaction_id=%s error=%s",
                request_id,
                interaction_id,
                exc,
            )
            emit_hard_error(
                event="discord_gateway_interaction_failed",
                error_ref=request_id[:8],
                exc=exc,
                context={"interaction_id": interaction_id},
            )

    def _handle_message_create(self, payload: dict, bot_token: str) -> None:
        request_id = str(payload.get("id") or "").strip() or str(uuid4())
        with self._session_factory() as session:
            result = build_discord_message_ingress_result(
                payload=payload,
                session=session,
                deps=self._message_dispatch_deps(bot_token=bot_token),
            )
        self._execute_ingress_result(
            result=result,
            envelope=TransportEnvelope(
                transport="discord_gateway",
                event_type="message_create",
                request_id=request_id,
                payload=payload,
            ),
            bot_token=bot_token,
        )

    def _handle_guild_member_add(self, payload: dict, bot_token: str) -> None:
        guild_id = str(payload.get("guild_id") or "").strip()
        user_payload = payload.get("user") if isinstance(payload.get("user"), dict) else {}
        discord_user_id = str(user_payload.get("id") or "").strip()
        if not guild_id or not discord_user_id:
            return

        with self._session_factory() as session:
            tenant = next(
                (
                    candidate
                    for candidate in session.execute(select(Tenant).where(Tenant.is_enabled.is_(True))).scalars().all()
                    if str((candidate.discord_config or {}).get("guild_id") or "").strip() == guild_id
                ),
                None,
            )
            if tenant is None:
                return

            identity = session.execute(
                select(TenantUserDiscordIdentity).where(TenantUserDiscordIdentity.discord_user_id == discord_user_id)
            ).scalar_one_or_none()
            if identity is None:
                return

            membership = session.execute(
                select(TenantMembership).where(
                    TenantMembership.tenant_id == tenant.tenant_id,
                    TenantMembership.user_id == identity.user_id,
                )
            ).scalar_one_or_none()
            if membership is None:
                return

            discord_state = dict(membership.discord_state or {})
            discord_state["linked"] = True
            discord_state["guild_joined"] = True
            discord_state["guild_joined_at"] = datetime.now(UTC).isoformat()
            current_status = str(discord_state.get("welcome_status") or "").strip().lower()
            if current_status == "sent":
                membership.discord_state = discord_state
                membership.updated_at = datetime.now(UTC)
                session.commit()
                return

            discord_state["welcome_status"] = "queued"
            membership.discord_state = discord_state
            membership.updated_at = datetime.now(UTC)
            session.flush()

            try:
                audio = synthesize_reply_audio(
                    settings=self._settings,
                    text=f"Welcome to {tenant.name}. You're set up and ready to build with the team.",
                )
                client = DiscordApiClient(bot_token=bot_token)
                dm_channel_id = client.create_dm_channel(user_id=discord_user_id)
                client.post_message_with_attachment(
                    channel_id=dm_channel_id,
                    content=f"Welcome to {tenant.name}.",
                    filename=audio.filename,
                    file_bytes=audio.audio_bytes,
                    content_type=audio.content_type,
                    flags=_VOICE_MESSAGE_FLAG,
                )
                discord_state["welcome_status"] = "sent"
                discord_state["welcome_sent_at"] = datetime.now(UTC).isoformat()
                discord_state["last_failure_reason"] = None
            except (DiscordApiError, VoiceReplyError, ValueError) as exc:
                discord_state["welcome_status"] = "failed"
                discord_state["last_failure_reason"] = str(exc)
                logger.exception(
                    "discord_gateway_welcome_dm_failed tenant_id=%s user_id=%s discord_user_id=%s error=%s",
                    tenant.tenant_id,
                    identity.user_id,
                    discord_user_id,
                    exc,
                )

            membership.discord_state = discord_state
            membership.updated_at = datetime.now(UTC)
            session.commit()

    def _transcribe_room_audio_attachment(
        self,
        *,
        attachment: dict[str, str],
        bot_token: str,
        correlation_id: str | None = None,
        tenant_id: str | None = None,
        project_id: str | None = None,
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
                with scoped_log_context(
                    correlation_id=correlation_id,
                    tenant_id=tenant_id,
                    project_id=project_id,
                ):
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
            with scoped_log_context(
                correlation_id=correlation_id,
                tenant_id=tenant_id,
                project_id=project_id,
            ):
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

    def _build_room_voice_reply_action(
        self,
        *,
        user_id: str,
        channel_id: str,
        text: str,
        persona_id: str | None = None,
        persona_name: str | None = None,
        persona_role: str | None = None,
        room_config: dict | None = None,
        content_override: str | None = None,
        components: list[dict] | None = None,
        correlation_id: str | None = None,
        tenant_id: str | None = None,
        project_id: str | None = None,
        fallback_content_on_failure: str | None = None,
        fallback_components_on_failure: list[dict] | None = None,
    ) -> tuple[TransportAction | None, str | None]:
        persona_label = format_voice_room_persona_label(
            persona_id=persona_id,
            persona_name=persona_name,
            persona_role=persona_role,
        )
        try:
            with scoped_log_context(
                correlation_id=correlation_id,
                tenant_id=tenant_id,
                project_id=project_id,
            ):
                audio = synthesize_reply_audio(
                    settings=self._settings,
                    text=build_voice_room_spoken_reply_text(
                        message=text,
                        persona_id=persona_id,
                        persona_name=persona_name,
                        persona_role=persona_role,
                    ),
                    persona_id=persona_id,
                    room_config=room_config,
                )
        except VoiceReplyError as exc:
            logger.warning("discord_gateway_room_voice_reply_failed channel_id=%s reason=%s", channel_id, exc)
            return None, f"Voice reply failed for `{persona_label}`: {exc}"

        attachment_content = str(content_override or "").strip()
        if not attachment_content:
            attachment_content = f"<@{user_id}> Voice reply from {persona_label}"
        return (
            DiscordChannelMessageWithAttachmentAction(
                channel_id=channel_id,
                content=attachment_content,
                filename=audio.filename,
                file_bytes=audio.audio_bytes,
                content_type=audio.content_type,
                components=components,
                fallback_content_on_failure=fallback_content_on_failure,
                fallback_components_on_failure=fallback_components_on_failure,
                failure_user_id=user_id,
            ),
            None,
        )

    def _post_room_voice_reply(
        self,
        *,
        bot_token: str,
        user_id: str,
        channel_id: str,
        text: str,
        persona_id: str | None = None,
        persona_name: str | None = None,
        persona_role: str | None = None,
        room_config: dict | None = None,
        content_override: str | None = None,
        components: list[dict] | None = None,
        correlation_id: str | None = None,
        tenant_id: str | None = None,
        project_id: str | None = None,
    ) -> str | None:
        action, error = self._build_room_voice_reply_action(
            user_id=user_id,
            channel_id=channel_id,
            text=text,
            persona_id=persona_id,
            persona_name=persona_name,
            persona_role=persona_role,
            room_config=room_config,
            content_override=content_override,
            components=components,
            correlation_id=correlation_id,
            tenant_id=tenant_id,
            project_id=project_id,
        )
        if action is None:
            return error
        try:
            execute_side_effect_ingress_result(
                result=IngressResult(actions=(action,)),
                envelope=TransportEnvelope(
                    transport="discord_gateway",
                    event_type="voice_reply",
                    request_id=correlation_id or str(uuid4()),
                    tenant_id_hint=tenant_id,
                ),
                transport_action_executors=self._transport_action_executors(bot_token=bot_token),
                action_error_handler=lambda failed_action, exc: self._handle_ingress_action_error(
                    action=failed_action,
                    exc=exc,
                    bot_token=bot_token,
                ),
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
