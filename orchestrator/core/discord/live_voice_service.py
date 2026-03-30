from __future__ import annotations

import base64
import asyncio
import io
import json
import logging
import re
import shutil
import threading
import time
import wave
from dataclasses import dataclass
from datetime import datetime, timezone
from collections.abc import Callable
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select

from orchestrator.api.discord.ask.context import tenant_project_keys
from orchestrator.api.discord.ingress.executor import execute_tenant_command_ingress
from orchestrator.api.schemas import DiscordCommandRequest, DiscordCommandResponse
from orchestrator.api.discord.shared.room_history import DiscordRoomHistoryService
from orchestrator.api.discord.shared.state import (
    live_voice_enabled_from_discord_config,
    live_voice_room_links_from_discord_config,
)
from orchestrator.api.discord.shared.state_repository import resolve_project_for_discord_channel
from orchestrator.core.codex_runtime import CodexRuntimeError
from orchestrator.core.codex_working_dir import resolve_codex_working_dir
from orchestrator.core.config import Settings
from orchestrator.core.discord.live_voice_audio import (
    LiveVoiceAudioError,
    ensure_opus_decoder_ready,
    build_opus_decoder,
    decode_opus_packets_to_pcm,
)
from orchestrator.core.discord.live_voice_runtime import build_live_voice_runtime
from orchestrator.core.discord.live_voice_session import (
    LiveVoiceCallbacks,
    LiveVoiceRoomBinding,
    LiveVoiceSessionState,
    LiveVoiceTurn,
)
from orchestrator.core.discord.live_voice_transport_client import (
    DEFAULT_TRANSPORT_SESSION_ID,
    LiveVoiceTransportClientError,
    LiveVoiceTransportRoom,
    build_live_voice_transport_client,
)
from orchestrator.core.discord.persona_room import VoiceRoomTurnResult
from orchestrator.core.discord.voice_entry_routing import route_discord_voice_entry
from orchestrator.core.discord.personas import (
    build_voice_room_config,
    build_voice_room_spoken_reply_text,
    format_voice_room_persona_label,
    resolve_voice_room_persona_profile,
)
from orchestrator.core.observability import scoped_log_context
from orchestrator.core.platform_secret_service import (
    PLATFORM_SECRET_DISCORD_BOT_TOKEN_REF,
    resolve_platform_secret_ref,
)
from orchestrator.core.voice.transcription import (
    VoiceTranscriptionError,
    ensure_transcription_provider_ready,
    transcribe_audio_bytes,
)
from orchestrator.core.voice.tts import VoiceReplyError, synthesize_reply_audio
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import Project, Tenant
from orchestrator.tools.discord_api import DiscordApiClient, DiscordApiError


logger = logging.getLogger("orchestrator.discord_live_voice")

_LIVE_VOICE_HISTORY_LIMIT = 8
_LIVE_VOICE_REASONING_EFFORT = "low"
_ISSUE_KEY_PATTERN = re.compile(r"\b[A-Z][A-Z0-9_]+-\d+\b")


class DiscordLiveVoiceDependencyFailure(RuntimeError):
    pass


@dataclass(frozen=True)
class ConfiguredLiveVoiceRoom:
    tenant_id: str
    guild_id: str
    voice_channel_id: str
    linked_text_channel_id: str
    project_id: str | None = None

    @property
    def binding(self) -> LiveVoiceRoomBinding:
        return LiveVoiceRoomBinding(
            guild_id=self.guild_id,
            voice_channel_id=self.voice_channel_id,
            text_channel_id=self.linked_text_channel_id,
        )

    @property
    def room_key(self) -> str:
        return self.binding.room_key


class DiscordLiveVoiceService:
    def __init__(
        self,
        *,
        settings: Settings,
        session_factory=None,
        secret_resolver=resolve_platform_secret_ref,
        discord_api_client_factory=DiscordApiClient,
        transport_client_factory: Callable[..., Any] = build_live_voice_transport_client,
        stop_event: threading.Event | None = None,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory or create_session_factory()
        self._secret_resolver = secret_resolver
        self._discord_api_client_factory = discord_api_client_factory
        self._transport_client_factory = transport_client_factory
        self._room_history = DiscordRoomHistoryService()
        self._runtime = build_live_voice_runtime(
            callbacks=LiveVoiceCallbacks(
                on_turn_discarded=self._on_turn_discarded,
            )
        )
        self._hikari: Any | None = None
        self._bot: Any | None = None
        self._bot_loop: asyncio.AbstractEventLoop | None = None
        self._transport_client: Any | None = None
        self._bot_user_id: str | None = None
        self._bot_token: str | None = None
        self._transport_session_id = DEFAULT_TRANSPORT_SESSION_ID
        self._transport_ready = False
        self._rooms_by_key: dict[str, ConfiguredLiveVoiceRoom] = {}
        self._synced_room_keys: set[str] = set()
        self._synced_room_signatures: dict[str, tuple[str, str, str, str, str]] = {}
        self._synced_rooms_by_key: dict[str, ConfiguredLiveVoiceRoom] = {}
        self._active_room_key_by_guild_id: dict[str, str] = {}
        self._connected_room_keys: set[str] = set()
        self._room_operation_locks: dict[str, asyncio.Lock] = {}
        self._decoder_by_key: dict[str, Any] = {}
        self._runtime_lock = threading.RLock()
        self._turn_worker_lock = threading.Lock()
        self._pending_turns: dict[str, tuple[int, LiveVoiceTurn] | None] = {}
        self._turn_events: dict[str, threading.Event] = {}
        self._turn_workers: dict[str, threading.Thread] = {}
        self._turn_versions: dict[str, int] = {}
        self._stop_event = stop_event or threading.Event()
        self._shutdown_thread: threading.Thread | None = None

    def run(self) -> None:
        try:
            ensure_transcription_provider_ready(settings=self._settings)
            ensure_opus_decoder_ready()
        except VoiceTranscriptionError as exc:
            raise DiscordLiveVoiceDependencyFailure(f"Live voice transcription is not ready: {exc}") from exc
        except LiveVoiceAudioError as exc:
            raise DiscordLiveVoiceDependencyFailure(str(exc)) from exc

        if str(self._settings.voice_provider or "").strip().lower() in {"", "disabled"}:
            raise DiscordLiveVoiceDependencyFailure(
                "Live voice requires ORCHESTRATOR_VOICE_PROVIDER to be configured."
            )
        if shutil.which("ffmpeg") is None:
            raise DiscordLiveVoiceDependencyFailure(
                "Live voice requires ffmpeg to be installed and available on PATH."
            )

        bot_token = self._resolve_bot_token()
        if not bot_token:
            raise DiscordLiveVoiceDependencyFailure("Discord bot token is not configured.")

        try:
            transport_client = self._transport_client_factory(
                settings=self._settings,
                bot_token=bot_token,
                event_handler=self._handle_transport_event,
            )
        except LiveVoiceTransportClientError as exc:
            raise DiscordLiveVoiceDependencyFailure(str(exc)) from exc

        self._transport_client = transport_client
        self._bot_token = bot_token
        self._start_transport_client()
        try:
            while not self._stop_event.is_set():
                self._refresh_room_registry()
                self._sync_rooms_with_sidecar()
                self._drain_finalized_turns()
                self._stop_event.wait(timeout=max(0.25, float(self._settings.discord_live_voice_poll_seconds)))
        finally:
            self._stop_event.set()
            self._close_transport_client()

    def _start_transport_client(self) -> None:
        transport_client = self._transport_client
        if transport_client is None:
            return
        set_event_handler = getattr(transport_client, "set_event_handler", None)
        if callable(set_event_handler):
            set_event_handler(self._handle_transport_event)
        start = getattr(transport_client, "start", None)
        if callable(start):
            start()

    def _close_transport_client(self) -> None:
        transport_client = self._transport_client
        if transport_client is None:
            return
        close = getattr(transport_client, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001
                logger.exception("discord_live_voice_transport_close_failed")

    def _decode_member_speech(
        self,
        room_key: str,
        user_id: str,
        packets: list[bytes],
    ) -> tuple[bytes, int, int]:
        decoder_key = f"{room_key}:{user_id}"
        decoder = self._decoder_by_key.get(decoder_key)
        if decoder is None:
            decoder = build_opus_decoder()
            self._decoder_by_key[decoder_key] = decoder
        try:
            return decode_opus_packets_to_pcm(packets=packets, decoder=decoder)
        except LiveVoiceAudioError:
            self._decoder_by_key.pop(decoder_key, None)
            raise

    def _handle_transport_disconnect(self, *, room: ConfiguredLiveVoiceRoom) -> None:
        self._connected_room_keys.discard(room.room_key)
        self._active_room_key_by_guild_id.pop(room.guild_id, None)
        self._drop_room_decoders(room_key=room.room_key)
        with self._turn_worker_lock:
            self._pending_turns.pop(room.room_key, None)
            wake_event = self._turn_events.get(room.room_key)
            if wake_event is not None:
                wake_event.set()
        with self._runtime_lock:
            session = self._runtime.get_session(binding=room.binding)
            if session is not None and session.state != LiveVoiceSessionState.DISCONNECTED:
                self._runtime.leave_room(binding=room.binding)

    def _handle_transport_event(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("type") or "").strip()
        session_id = str(event.get("session_id") or "").strip() or self._transport_session_id
        if session_id != self._transport_session_id:
            logger.info("discord_live_voice_transport_event_ignored reason=unknown_session type=%s session_id=%s", event_type, session_id)
            return

        if event_type == "transport_ready":
            self._transport_ready = True
            logger.info("discord_live_voice_transport_ready session_id=%s backend=%s", session_id, event.get("backend"))
            return

        if event_type == "transport_failed":
            stage = str(event.get("stage") or "").strip()
            retryable = bool(event.get("retryable"))
            if not retryable or stage in {"process_exit", "open_session", "client_new", "open_gateway"}:
                self._transport_ready = False
            logger.warning(
                "discord_live_voice_transport_failed session_id=%s stage=%s error=%s retryable=%s",
                session_id,
                stage,
                event.get("error"),
                retryable,
            )
            return

        room = self._room_for_transport_event(event=event)
        if room is None:
            logger.info("discord_live_voice_transport_event_ignored reason=unmapped_room type=%s session_id=%s", event_type, session_id)
            return

        if event_type == "room_state":
            self._apply_room_state_event(room=room, event=event)
            return

        if event_type == "opus_frame":
            self._handle_opus_frame_event(room=room, event=event)
            return

        if event_type == "playback_finished":
            with self._runtime_lock:
                session = self._runtime.get_session(binding=room.binding)
                if session is not None and session.bot_speaking:
                    self._runtime.mark_bot_speaking(binding=room.binding, speaking=False)
            logger.info(
                "discord_live_voice_playback_finished room_key=%s playback_frames=%s",
                room.room_key,
                event.get("playback_frames"),
            )
            return

        logger.info(
            "discord_live_voice_transport_event_ignored type=%s room_key=%s",
            event_type,
            room.room_key,
        )

    def _sync_rooms_with_sidecar(self) -> None:
        transport_client = self._transport_client
        if transport_client is None:
            return

        removed_room_keys = self._synced_room_keys - set(self._rooms_by_key)
        for room_key in sorted(removed_room_keys):
            room = self._synced_rooms_by_key.get(room_key)
            if room is not None:
                self._handle_transport_disconnect(room=room)
            self._synced_room_signatures.pop(room_key, None)
            self._synced_rooms_by_key.pop(room_key, None)

        for room in self._rooms_by_key.values():
            with self._runtime_lock:
                if self._runtime.get_session(binding=room.binding) is None:
                    self._runtime.register_room(binding=room.binding, bot_user_id=None, human_member_count=0)

        rooms = [
            LiveVoiceTransportRoom(guild_id=room.guild_id, channel_id=room.voice_channel_id)
            for room in self._rooms_by_key.values()
        ]
        try:
            transport_client.sync_session(
                session_id=self._transport_session_id,
                bot_token=self._bot_token or "",
                rooms=rooms,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("discord_live_voice_transport_sync_failed error=%s", exc)
            return

        self._synced_room_keys = set(self._rooms_by_key)
        self._synced_rooms_by_key = dict(self._rooms_by_key)
        self._synced_room_signatures = {
            room_key: self._room_sync_signature(room=room) for room_key, room in self._rooms_by_key.items()
        }

    @staticmethod
    def _room_sync_signature(*, room: ConfiguredLiveVoiceRoom) -> tuple[str, str, str, str, str]:
        return (
            room.tenant_id,
            room.guild_id,
            room.voice_channel_id,
            room.linked_text_channel_id,
            room.project_id or "",
        )

    def _room_for_transport_event(self, *, event: dict[str, Any]) -> ConfiguredLiveVoiceRoom | None:
        guild_id = str(event.get("guild_id") or "").strip()
        channel_id = str(event.get("channel_id") or "").strip()
        if not guild_id or not channel_id:
            return None
        return self._room_for_channel(guild_id=guild_id, channel_id=channel_id)

    def _apply_room_state_event(self, *, room: ConfiguredLiveVoiceRoom, event: dict[str, Any]) -> None:
        joined = bool(event.get("joined"))
        human_count = max(0, int(event.get("human_count") or 0))
        if joined:
            self._connected_room_keys.add(room.room_key)
            self._active_room_key_by_guild_id[room.guild_id] = room.room_key
            self._mark_room_joined(room=room, human_count=human_count)
            logger.info("discord_live_voice_room_joined room_key=%s human_count=%s", room.room_key, human_count)
            return

        self._handle_transport_disconnect(room=room)
        logger.info("discord_live_voice_room_left room_key=%s human_count=%s", room.room_key, human_count)

    def _handle_opus_frame_event(self, *, room: ConfiguredLiveVoiceRoom, event: dict[str, Any]) -> None:
        encoded_frame = str(event.get("opus_frame_base64") or "").strip()
        user_id = str(event.get("user_id") or "").strip()
        if not encoded_frame or not user_id:
            logger.info("discord_live_voice_opus_frame_ignored room_key=%s reason=missing_payload", room.room_key)
            return
        if user_id == "0":
            logger.info(
                "discord_live_voice_opus_frame_ignored room_key=%s reason=unknown_user",
                room.room_key,
            )
            return
        try:
            opus_packet = base64.b64decode(encoded_frame.encode("ascii"))
        except Exception:  # noqa: BLE001
            logger.exception("discord_live_voice_opus_frame_decode_failed room_key=%s user_id=%s", room.room_key, user_id)
            return

        try:
            pcm_bytes, sample_rate_hz, channels = self._decode_member_speech(room.room_key, user_id, [opus_packet])
        except LiveVoiceAudioError as exc:
            logger.warning(
                "discord_live_voice_opus_frame_ignored room_key=%s user_id=%s reason=decode_failed error=%s",
                room.room_key,
                user_id,
                exc,
            )
            return
        if not pcm_bytes:
            return

        received_at = datetime.now(timezone.utc)
        with self._runtime_lock:
            session = self._runtime.get_session(binding=room.binding)
            should_interrupt = bool(session is not None and session.bot_speaking)
        if should_interrupt:
            self._interrupt_playback(room=room, reason="user_audio")

        with self._runtime_lock:
            turn = self._runtime.ingest_audio(
                binding=room.binding,
                user_id=user_id,
                audio_bytes=pcm_bytes,
                received_at=received_at,
                sample_rate_hz=sample_rate_hz,
                channels=channels,
                is_bot_audio=False,
            )
        if turn is not None:
            self._start_turn_processing(turn=turn)

    def _drain_finalized_turns(self) -> None:
        with self._runtime_lock:
            finalized_turns = self._runtime.poll(now=datetime.now(timezone.utc))
        for turn in finalized_turns:
            self._start_turn_processing(turn=turn)

    def _start_turn_processing(self, *, turn: LiveVoiceTurn) -> None:
        room_key = turn.room_key
        room = self._rooms_by_key.get(room_key)
        if room is not None:
            with self._runtime_lock:
                session = self._runtime.get_session(binding=room.binding)
                should_interrupt = bool(session is not None and session.bot_speaking)
            if should_interrupt:
                self._interrupt_playback(room=room, reason="new_turn_finalized")
        with self._turn_worker_lock:
            next_version = self._turn_versions.get(room_key, 0) + 1
            self._turn_versions[room_key] = next_version
            self._pending_turns[room_key] = (next_version, turn)
            wake_event = self._turn_events.get(room_key)
            if wake_event is None:
                wake_event = threading.Event()
                self._turn_events[room_key] = wake_event
            wake_event.set()
            worker = self._turn_workers.get(room_key)
            if worker is not None and worker.is_alive():
                return
            worker = threading.Thread(
                target=self._process_room_turn_queue,
                kwargs={"room_key": room_key},
                name=f"discord-live-voice-room-{room_key}",
                daemon=True,
            )
            self._turn_workers[room_key] = worker
            worker.start()

    def _process_room_turn_queue(self, *, room_key: str) -> None:
        while not self._stop_event.is_set():
            with self._turn_worker_lock:
                wake_event = self._turn_events.get(room_key)
            if wake_event is None:
                return
            if not wake_event.wait(timeout=0.5):
                with self._turn_worker_lock:
                    pending = self._pending_turns.get(room_key)
                    latest_event = self._turn_events.get(room_key)
                    if pending is None and latest_event is wake_event and not latest_event.is_set():
                        self._turn_workers.pop(room_key, None)
                        self._turn_events.pop(room_key, None)
                        self._pending_turns.pop(room_key, None)
                        return
                continue

            with self._turn_worker_lock:
                pending = self._pending_turns.get(room_key)
                if pending is None:
                    wake_event.clear()
                    continue
                version, turn = pending
                self._pending_turns[room_key] = None
                wake_event.clear()
            try:
                self._process_turn(turn=turn, turn_version=version)
            except Exception:  # noqa: BLE001
                logger.exception("discord_live_voice_turn_processing_failed room_key=%s", room_key)

    def _refresh_room_registry(self) -> None:
        with self._session_factory() as session:
            tenants = session.execute(
                select(Tenant).where(Tenant.is_enabled.is_(True))
            ).scalars().all()
            tenant_by_id = {str(tenant.tenant_id): tenant for tenant in tenants}
            refreshed: dict[str, ConfiguredLiveVoiceRoom] = {}

            for tenant in tenants:
                guild_id = _tenant_guild_id(tenant=tenant, settings=self._settings)
                if not guild_id or not live_voice_enabled_from_discord_config(tenant.discord_config):
                    continue
                for voice_channel_id, linked_text_channel_id in live_voice_room_links_from_discord_config(
                    tenant.discord_config
                ).items():
                    project = resolve_project_for_discord_channel(
                        session=session,
                        tenant_id=tenant.tenant_id,
                        channel_id=linked_text_channel_id,
                    )
                    room = ConfiguredLiveVoiceRoom(
                        tenant_id=tenant.tenant_id,
                        project_id=getattr(project, "project_id", None),
                        guild_id=guild_id,
                        voice_channel_id=voice_channel_id,
                        linked_text_channel_id=linked_text_channel_id,
                    )
                    refreshed[room.room_key] = room

            projects = session.execute(
                select(Project).where(Project.is_archived.is_(False))
            ).scalars().all()
            for project in projects:
                tenant = tenant_by_id.get(str(project.tenant_id))
                if tenant is None:
                    continue
                guild_id = _tenant_guild_id(tenant=tenant, settings=self._settings)
                if not guild_id or not live_voice_enabled_from_discord_config(project.discord_config):
                    continue
                for voice_channel_id, linked_text_channel_id in live_voice_room_links_from_discord_config(
                    project.discord_config
                ).items():
                    room = ConfiguredLiveVoiceRoom(
                        tenant_id=tenant.tenant_id,
                        project_id=project.project_id,
                        guild_id=guild_id,
                        voice_channel_id=voice_channel_id,
                        linked_text_channel_id=linked_text_channel_id,
                    )
                    refreshed[room.room_key] = room

            self._rooms_by_key = refreshed
            for room in refreshed.values():
                with self._runtime_lock:
                    if self._runtime.get_session(binding=room.binding) is None:
                        self._runtime.register_room(
                            binding=room.binding,
                            bot_user_id=self._bot_user_id,
                            human_member_count=0,
                        )

    def _mark_room_joined(self, *, room: ConfiguredLiveVoiceRoom, human_count: int) -> None:
        with self._runtime_lock:
            session = self._runtime.get_session(binding=room.binding)
            if session is None:
                self._runtime.register_room(
                    binding=room.binding,
                    bot_user_id=self._bot_user_id,
                    human_member_count=human_count,
                )
                session = self._runtime.get_session(binding=room.binding)
            if session is not None and session.state == LiveVoiceSessionState.DISCONNECTED:
                self._runtime.join_room(binding=room.binding, human_member_count=human_count)

    def _room_for_channel(self, *, guild_id: str, channel_id: str) -> ConfiguredLiveVoiceRoom | None:
        normalized_guild_id = str(guild_id or "").strip()
        normalized_channel_id = str(channel_id or "").strip()
        for room in self._rooms_by_key.values():
            if room.guild_id == normalized_guild_id and room.voice_channel_id == normalized_channel_id:
                return room
        return None

    def _drop_room_decoders(self, *, room_key: str) -> None:
        for key in list(self._decoder_by_key):
            if key.startswith(f"{room_key}:"):
                self._decoder_by_key.pop(key, None)

    def _interrupt_playback(self, *, room: ConfiguredLiveVoiceRoom, reason: str) -> None:
        transport_client = self._transport_client
        if transport_client is None:
            return
        stop_audio = getattr(transport_client, "stop_audio", None)
        if callable(stop_audio):
            try:
                stop_audio(binding=room.binding)
            except Exception:  # noqa: BLE001
                logger.exception("discord_live_voice_stop_audio_failed room_key=%s reason=%s", room.room_key, reason)
        with self._runtime_lock:
            session = self._runtime.get_session(binding=room.binding)
            if session is not None and session.bot_speaking:
                self._runtime.mark_bot_speaking(binding=room.binding, speaking=False)
        logger.info("discord_live_voice_playback_interrupted room_key=%s reason=%s", room.room_key, reason)

    def _collect_live_voice_context(
        self,
        *,
        session,  # noqa: ANN001
        tenant: Tenant,
        project: Project | None,
        transcript: str,
    ) -> tuple[str | None, str | None, list[str], str]:
        normalized_issue_key = None
        match = _ISSUE_KEY_PATTERN.search(str(transcript or ""))
        if match is not None:
            normalized_issue_key = str(match.group(0)).strip().upper() or None
        project_keys = (
            [str(project.jira_project_key).strip().upper()]
            if project is not None and str(project.jira_project_key).strip()
            else tenant_project_keys(session=session, tenant=tenant)
        )
        requested_status = None
        codex_working_dir = resolve_codex_working_dir(
            session=session,
            tenant=tenant,
            settings=self._settings,
            project_id=getattr(project, "project_id", None),
            project_keys=project_keys,
        )
        return normalized_issue_key, requested_status, project_keys, codex_working_dir

    @staticmethod
    def _live_voice_turn_from_discord_response(
        resp: DiscordCommandResponse,
        *,
        entry_confidence: float,
        entry_reason: str,
        tenant_discord_config: dict,
        project_discord_config: dict | None,
    ) -> VoiceRoomTurnResult:
        data = resp.data if isinstance(resp.data, dict) else {}
        persona_id = str(data.get("persona_id") or "pm").strip().lower() or "pm"
        profile = resolve_voice_room_persona_profile(
            persona_id=persona_id,
            tenant_discord_config=tenant_discord_config,
            project_discord_config=project_discord_config,
        )
        brief = data.get("brief") if isinstance(data.get("brief"), dict) else {}
        try:
            rc = float(entry_confidence)
        except (TypeError, ValueError):
            rc = 0.0
        rc = max(0.0, min(1.0, rc))
        return VoiceRoomTurnResult(
            persona_id=profile.persona_id,
            persona_role=profile.role_label,
            persona_name=profile.display_name,
            persona_voice_id=profile.voice_id,
            message=str(resp.message or "").strip(),
            brief=brief,
            router_confidence=rc,
            router_reason=str(entry_reason or "").strip(),
            room_config=build_voice_room_config(tenant_discord_config, project_discord_config),
        )

    def _turn_version_is_current(self, *, room_key: str, turn_version: int) -> bool:
        with self._turn_worker_lock:
            return self._turn_versions.get(room_key) == turn_version

    @staticmethod
    def _live_voice_turn_correlation_id(
        *,
        room: ConfiguredLiveVoiceRoom,
        turn: LiveVoiceTurn,
        turn_version: int,
    ) -> str:
        return (
            f"live-voice:{room.room_key}:{turn.user_id}:{turn.turn_index}:{turn_version}"
        )

    @staticmethod
    def _live_voice_reply_correlation_id(
        *,
        room: ConfiguredLiveVoiceRoom,
        result,  # noqa: ANN001
        turn_version: int,
    ) -> str:
        persona_id = str(getattr(result, "persona_id", "") or "").strip() or "unknown"
        return f"live-voice-reply:{room.room_key}:{turn_version}:{persona_id}"

    def _process_turn(self, *, turn: LiveVoiceTurn, turn_version: int) -> None:
        room = self._rooms_by_key.get(turn.room_key)
        if room is None or self._bot_token is None:
            logger.info(
                "discord_live_voice_turn_skipped room_key=%s reason=missing_room_or_bot_token",
                turn.room_key,
            )
            return
        self._process_turn_locked(room=room, turn=turn, turn_version=turn_version)

    def _process_turn_locked(self, *, room: ConfiguredLiveVoiceRoom, turn: LiveVoiceTurn, turn_version: int) -> None:
        transcript: str | None = None
        transcription_started_at = time.perf_counter()
        logger.info(
            "discord_live_voice_turn_processing_started room_key=%s user_id=%s audio_bytes=%s sample_rate_hz=%s channels=%s",
            room.room_key,
            turn.user_id,
            len(turn.audio_bytes),
            turn.sample_rate_hz,
            turn.channels,
        )
        with self._session_factory() as session:
            tenant = session.get(Tenant, room.tenant_id)
            if tenant is None or not tenant.is_enabled:
                logger.info(
                    "discord_live_voice_turn_skipped room_key=%s reason=tenant_unavailable",
                    room.room_key,
                )
                return
            project = session.get(Project, room.project_id) if room.project_id else resolve_project_for_discord_channel(
                session=session,
                tenant_id=tenant.tenant_id,
                channel_id=room.linked_text_channel_id,
            )
            scoped_project_id = str(getattr(project, "project_id", "") or "").strip() or None
            turn_correlation_id = self._live_voice_turn_correlation_id(
                room=room,
                turn=turn,
                turn_version=turn_version,
            )

            wav_bytes = _encode_pcm_wav(
                pcm_bytes=turn.audio_bytes,
                sample_rate_hz=turn.sample_rate_hz,
                channels=turn.channels,
            )
            try:
                with scoped_log_context(
                    correlation_id=turn_correlation_id,
                    tenant_id=tenant.tenant_id,
                    project_id=scoped_project_id,
                ):
                    transcript = transcribe_audio_bytes(
                        settings=self._settings,
                        audio_bytes=wav_bytes,
                        filename="live-voice.wav",
                        content_type="audio/wav",
                    )
            except VoiceTranscriptionError as exc:
                self._post_text_notice(
                    channel_id=room.linked_text_channel_id,
                    content=f"Live voice transcription failed: {exc}",
                )
                return
            if not transcript:
                logger.info(
                    "discord_live_voice_turn_skipped room_key=%s reason=empty_transcript",
                    room.room_key,
                )
                self._post_text_notice(
                    channel_id=room.linked_text_channel_id,
                    content="Live voice transcription returned no text.",
                )
                return
            logger.info(
                "discord_live_voice_turn_transcribed room_key=%s user_id=%s transcript_chars=%s transcription_ms=%s",
                room.room_key,
                turn.user_id,
                len(transcript),
                round((time.perf_counter() - transcription_started_at) * 1000, 2),
            )

            context_started_at = time.perf_counter()
            normalized_issue_key, requested_status, project_keys, codex_working_dir = self._collect_live_voice_context(
                session=session,
                tenant=tenant,
                project=project,
                transcript=transcript,
            )
            logger.info(
                "discord_live_voice_turn_context_ready room_key=%s user_id=%s context_ms=%s issue_key=%s",
                room.room_key,
                turn.user_id,
                round((time.perf_counter() - context_started_at) * 1000, 2),
                normalized_issue_key or "",
            )
            history_owner = project if project is not None else tenant

            pre_history = self._room_history.recent_room_history(
                discord_config=getattr(history_owner, "discord_config", None),
                linked_text_channel_id=room.linked_text_channel_id,
                voice_channel_id=room.voice_channel_id,
            )
            trimmed_for_router = pre_history[-_LIVE_VOICE_HISTORY_LIMIT:]

            routed = route_discord_voice_entry(
                session=session,
                settings=self._settings,
                tenant=tenant,
                project_id=scoped_project_id,
                codex_working_dir=codex_working_dir,
                transcript=transcript,
                entry_source="live_voice",
                history=trimmed_for_router,
                room_context={
                    "project_keys": project_keys,
                    "linked_text_channel_id": room.linked_text_channel_id,
                    "voice_channel_id": room.voice_channel_id,
                },
            )
            lane = str(routed.get("lane") or "ask").strip().lower()
            entry_persona = str(routed.get("persona") or "pm").strip().lower()
            conf = float(routed.get("confidence") or 0.0)
            reason = str(routed.get("reason") or "").strip()
            logger.info(
                "discord_live_voice_entry_routed lane=%s persona=%s confidence=%s reason=%s",
                lane,
                entry_persona,
                conf,
                reason,
            )

            live_params = {
                "room_mode": "true",
                "room_source": "live_voice",
                "linked_text_channel_id": room.linked_text_channel_id,
                "voice_channel_id": room.voice_channel_id,
            }

            tenant_dc = getattr(tenant, "discord_config", None) or {}
            project_dc = getattr(project, "discord_config", None) if project is not None else None

            answer_started_at = time.perf_counter()
            result: VoiceRoomTurnResult
            try:
                with scoped_log_context(
                    correlation_id=turn_correlation_id,
                    tenant_id=tenant.tenant_id,
                    project_id=scoped_project_id,
                ):
                    if lane == "interview":
                        resp = execute_tenant_command_ingress(
                            tenant_id=tenant.tenant_id,
                            payload=DiscordCommandRequest(
                                user_id=turn.user_id,
                                channel_id=room.linked_text_channel_id,
                                command=f"!pm {transcript}",
                                command_params=live_params,
                            ),
                            session=session,
                            require_ask_confirmation=False,
                            ingress_source="discord",
                        )
                        result = self._live_voice_turn_from_discord_response(
                            resp,
                            entry_confidence=conf,
                            entry_reason=reason,
                            tenant_discord_config=tenant_dc,
                            project_discord_config=project_dc,
                        )
                    else:
                        ask_params = {**live_params, "persona_id": entry_persona}
                        resp = execute_tenant_command_ingress(
                            tenant_id=tenant.tenant_id,
                            payload=DiscordCommandRequest(
                                user_id=turn.user_id,
                                channel_id=room.linked_text_channel_id,
                                command=f"!ask {transcript}",
                                command_params=ask_params,
                            ),
                            session=session,
                            require_ask_confirmation=False,
                            ingress_source="discord",
                        )
                        result = self._live_voice_turn_from_discord_response(
                            resp,
                            entry_confidence=conf,
                            entry_reason=reason,
                            tenant_discord_config=tenant_dc,
                            project_discord_config=project_dc,
                        )
                        session.refresh(history_owner)
                        history_config, _ = self._room_history.append_room_history_entry(
                            discord_config=getattr(history_owner, "discord_config", None),
                            room_id=room.linked_text_channel_id,
                            channel_id=room.linked_text_channel_id,
                            voice_channel_id=room.voice_channel_id,
                            linked_text_channel_id=room.linked_text_channel_id,
                            speaker_type="user",
                            source_mode="live_voice",
                            text=transcript,
                            user_id=turn.user_id,
                            issue_key=normalized_issue_key,
                            status_name=requested_status,
                            metadata={"finalization_reason": turn.finalization_reason, "voice_entry_lane": "ask"},
                        )
                        _set_discord_config(target=history_owner, discord_config=history_config)
                        history_config, _ = self._room_history.append_room_history_entry(
                            discord_config=getattr(history_owner, "discord_config", None),
                            room_id=room.linked_text_channel_id,
                            channel_id=room.linked_text_channel_id,
                            voice_channel_id=room.voice_channel_id,
                            linked_text_channel_id=room.linked_text_channel_id,
                            speaker_type="persona",
                            source_mode="live_voice",
                            text=result.message,
                            persona_id=result.persona_id,
                            issue_key=normalized_issue_key,
                            status_name=requested_status,
                            metadata={
                                "router_confidence": result.router_confidence,
                                "router_reason": result.router_reason,
                                "voice_entry_lane": "ask",
                            },
                        )
                        _set_discord_config(target=history_owner, discord_config=history_config)
            except HTTPException as exc:
                session.rollback()
                self._post_text_notice(
                    channel_id=room.linked_text_channel_id,
                    content=f"Live voice command failed: {exc.detail}",
                )
                return
            except CodexRuntimeError as exc:
                session.rollback()
                self._post_text_notice(
                    channel_id=room.linked_text_channel_id,
                    content=f"Live voice assistant error: {exc}",
                )
                return
            logger.info(
                "discord_live_voice_turn_answer_generated room_key=%s persona_id=%s answer_ms=%s",
                room.room_key,
                result.persona_id,
                round((time.perf_counter() - answer_started_at) * 1000, 2),
            )

            if not self._turn_version_is_current(room_key=room.room_key, turn_version=turn_version):
                logger.info(
                    "discord_live_voice_turn_skipped room_key=%s turn_version=%s reason=superseded",
                    room.room_key,
                    turn_version,
                )
                session.commit()
                return

            session.commit()

        logger.info(
            "discord_live_voice_turn_answered room_key=%s persona_id=%s response_chars=%s",
            room.room_key,
            result.persona_id,
            len(result.message),
        )
        self._schedule_persona_reply(room=room, result=result, turn_version=turn_version)

    def _schedule_persona_reply(self, *, room: ConfiguredLiveVoiceRoom, result, turn_version: int) -> None:  # noqa: ANN001
        loop = self._bot_loop
        if loop is None:
            threading.Thread(
                target=self._run_persona_reply_task,
                kwargs={"room": room, "result": result, "turn_version": turn_version},
                name=f"discord-live-voice-playback-{room.room_key}",
                daemon=True,
            ).start()
            return

        future = asyncio.run_coroutine_threadsafe(
            self._play_persona_reply(room=room, result=result, turn_version=turn_version),
            loop,
        )
        future.add_done_callback(lambda completed: self._handle_playback_future(room=room, future=completed))

    def _run_persona_reply_task(self, *, room: ConfiguredLiveVoiceRoom, result, turn_version: int) -> None:  # noqa: ANN001
        try:
            asyncio.run(self._play_persona_reply(room=room, result=result, turn_version=turn_version))
        except Exception as exc:  # noqa: BLE001
            logger.exception("discord_live_voice_playback_task_failed room_key=%s", room.room_key)
            self._post_text_notice(
                channel_id=room.linked_text_channel_id,
                content=f"Live voice playback failed: {exc}",
            )

    def _handle_playback_future(self, *, room: ConfiguredLiveVoiceRoom, future) -> None:  # noqa: ANN001
        try:
            future.result()
        except Exception as exc:  # noqa: BLE001
            logger.exception("discord_live_voice_playback_task_failed room_key=%s", room.room_key)
            self._post_text_notice(
                channel_id=room.linked_text_channel_id,
                content=f"Live voice playback failed: {exc}",
            )

    async def _play_persona_reply(self, *, room: ConfiguredLiveVoiceRoom, result, turn_version: int) -> None:  # noqa: ANN001
        if self._transport_client is None:
            self._post_text_notice(
                channel_id=room.linked_text_channel_id,
                content="Live voice reply failed: the live voice transport is not running.",
            )
            return
        if not self._turn_version_is_current(room_key=room.room_key, turn_version=turn_version):
            logger.info(
                "discord_live_voice_playback_skipped room_key=%s turn_version=%s reason=superseded_before_tts",
                room.room_key,
                turn_version,
            )
            return

        tts_started_at = time.perf_counter()
        reply_correlation_id = self._live_voice_reply_correlation_id(
            room=room,
            result=result,
            turn_version=turn_version,
        )
        try:
            with scoped_log_context(
                correlation_id=reply_correlation_id,
                tenant_id=room.tenant_id,
                project_id=room.project_id,
            ):
                audio = synthesize_reply_audio(
                    settings=self._settings,
                    text=build_voice_room_spoken_reply_text(
                        message=result.message,
                        persona_id=result.persona_id,
                        persona_name=result.persona_name,
                        persona_role=getattr(result, "persona_role", None),
                    ),
                    persona_id=result.persona_id,
                    room_config=result.room_config,
                )
        except (VoiceReplyError, LiveVoiceAudioError) as exc:
            persona_label = format_voice_room_persona_label(
                persona_id=result.persona_id,
                persona_name=result.persona_name,
                persona_role=getattr(result, "persona_role", None),
            )
            self._post_text_notice(
                channel_id=room.linked_text_channel_id,
                content=f"Live voice reply failed for {persona_label}: {exc}",
            )
            return
        logger.info(
            "discord_live_voice_tts_ready room_key=%s persona_id=%s tts_ms=%s",
            room.room_key,
            result.persona_id,
            round((time.perf_counter() - tts_started_at) * 1000, 2),
        )
        if not self._turn_version_is_current(room_key=room.room_key, turn_version=turn_version):
            logger.info(
                "discord_live_voice_playback_skipped room_key=%s turn_version=%s reason=superseded_before_playback",
                room.room_key,
                turn_version,
            )
            return

        with self._runtime_lock:
            session = self._runtime.get_session(binding=room.binding)
            if session is not None and not session.bot_speaking:
                self._runtime.mark_bot_speaking(binding=room.binding, speaking=True)

        playback_started_at = time.perf_counter()
        try:
            with scoped_log_context(
                correlation_id=reply_correlation_id,
                tenant_id=room.tenant_id,
                project_id=room.project_id,
            ):
                await asyncio.to_thread(
                    self._transport_client.play_audio,
                    binding=room.binding,
                    audio_bytes=audio.audio_bytes,
                    content_type="audio/wav",
                    metadata={
                        "persona_id": result.persona_id,
                        "persona_name": result.persona_name,
                        "room_key": room.room_key,
                    },
                )
            logger.info(
                "discord_live_voice_playback_dispatched room_key=%s persona_id=%s playback_dispatch_ms=%s",
                room.room_key,
                result.persona_id,
                round((time.perf_counter() - playback_started_at) * 1000, 2),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("discord_live_voice_playback_failed room_key=%s persona_id=%s", room.room_key, result.persona_id)
            persona_label = format_voice_room_persona_label(
                persona_id=result.persona_id,
                persona_name=result.persona_name,
                persona_role=getattr(result, "persona_role", None),
            )
            self._post_text_notice(
                channel_id=room.linked_text_channel_id,
                content=f"Live voice reply failed for {persona_label}: {exc}",
            )
            with self._runtime_lock:
                session = self._runtime.get_session(binding=room.binding)
                if session is not None and session.bot_speaking:
                    self._runtime.mark_bot_speaking(binding=room.binding, speaking=False)

    def _post_text_notice(self, *, channel_id: str, content: str) -> None:
        if not self._bot_token:
            return
        try:
            self._discord_api_client_factory(bot_token=self._bot_token).post_message(
                channel_id=channel_id,
                content=content,
            )
        except (DiscordApiError, ValueError) as exc:
            logger.exception("discord_live_voice_text_post_failed channel_id=%s error=%s", channel_id, exc)

    def _resolve_bot_token(self) -> str | None:
        with self._session_factory() as session:
            return self._secret_resolver(
                session,
                secret_ref=PLATFORM_SECRET_DISCORD_BOT_TOKEN_REF,
                encryption_key=str(getattr(self._settings, "secrets_encryption_key", "") or "").strip(),
                allow_environment_fallback=True,
            )

    def _on_turn_discarded(self, *, session, reason: str) -> None:  # noqa: ANN001
        logger.warning("discord_live_voice_turn_discarded room_key=%s reason=%s", session.room_key, reason)


def _tenant_guild_id(*, tenant: Tenant, settings: Settings) -> str | None:
    tenant_discord_config = getattr(tenant, "discord_config", None) or {}
    guild_id = str(tenant_discord_config.get("guild_id") or "").strip()
    if guild_id:
        return guild_id
    return None


def _set_discord_config(*, target: Tenant | Project, discord_config: dict[str, Any]) -> None:
    target.discord_config = dict(discord_config)
    if hasattr(target, "updated_at"):
        target.updated_at = datetime.now(timezone.utc)


def _encode_pcm_wav(*, pcm_bytes: bytes, sample_rate_hz: int, channels: int) -> bytes:
    if not pcm_bytes:
        raise ValueError("PCM audio payload cannot be empty")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(max(1, int(channels)))
        wav_file.setsampwidth(2)
        wav_file.setframerate(max(8_000, int(sample_rate_hz)))
        wav_file.writeframes(pcm_bytes)
    return buffer.getvalue()
