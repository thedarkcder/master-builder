from __future__ import annotations

import asyncio
import io
import logging
import threading
import wave
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import import_module
from typing import Any

from sqlalchemy import select

from orchestrator.api.discord.ask.context import tenant_project_keys
from orchestrator.api.discord.ingress.ask_runtime import collect_ask_context, collect_github_ask_context
from orchestrator.api.discord.shared.room_history import DiscordRoomHistoryService
from orchestrator.api.discord.shared.state import (
    live_voice_enabled_from_discord_config,
    live_voice_room_links_from_discord_config,
)
from orchestrator.api.discord.shared.state_repository import resolve_project_for_discord_channel
from orchestrator.core.codex_invocation import CodexInvocationContext
from orchestrator.core.codex_runtime import CodexRuntimeError, build_codex_runtime
from orchestrator.core.codex_working_dir import resolve_codex_working_dir
from orchestrator.core.config import Settings
from orchestrator.core.discord.live_voice_runtime import build_live_voice_runtime
from orchestrator.core.discord.live_voice_session import LiveVoiceCallbacks, LiveVoiceRoomBinding, LiveVoiceTurn
from orchestrator.core.discord.persona_room import answer_voice_room_turn
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

_DISCORD_PCM_FRAME_BYTES = 3_840
_VOICE_POLL_INTERVAL_SECONDS = 0.25


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


class _DiscordPCMSource:
    def __init__(self, *, pcm_bytes: bytes):
        self._buffer = io.BytesIO(pcm_bytes)

    def read(self) -> bytes:
        return self._buffer.read(_DISCORD_PCM_FRAME_BYTES)

    def is_opus(self) -> bool:
        return False

    def cleanup(self) -> None:
        return None


class DiscordLiveVoiceService:
    def __init__(
        self,
        *,
        settings: Settings,
        session_factory=None,
        secret_resolver=resolve_platform_secret_ref,
        discord_api_client_factory=DiscordApiClient,
        stop_event: threading.Event | None = None,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory or create_session_factory()
        self._secret_resolver = secret_resolver
        self._discord_api_client_factory = discord_api_client_factory
        self._room_history = DiscordRoomHistoryService()
        self._runtime = build_live_voice_runtime(
            callbacks=LiveVoiceCallbacks(
                on_turn_discarded=self._on_turn_discarded,
            )
        )
        self._discord_module: Any | None = None
        self._discord_client: Any | None = None
        self._bot_token: str | None = None
        self._rooms_by_key: dict[str, ConfiguredLiveVoiceRoom] = {}
        self._voice_clients_by_room_key: dict[str, Any] = {}
        self._voice_clients_by_guild_id: dict[str, str] = {}
        self._runtime_lock = threading.RLock()
        self._processing_lock = threading.Lock()
        self._stop_event = stop_event or threading.Event()
        self._poll_thread: threading.Thread | None = None
        self._shutdown_thread: threading.Thread | None = None

    def run(self) -> None:
        try:
            ensure_transcription_provider_ready(settings=self._settings)
        except VoiceTranscriptionError as exc:
            raise DiscordLiveVoiceDependencyFailure(f"Live voice transcription is not ready: {exc}") from exc
        if str(self._settings.voice_reply_provider or "").strip().lower() in {"", "disabled"}:
            raise DiscordLiveVoiceDependencyFailure(
                "Live voice requires ORCHESTRATOR_VOICE_REPLY_PROVIDER to be configured."
            )

        discord = _load_discord_runtime()
        bot_token = self._resolve_bot_token()
        if not bot_token:
            raise DiscordLiveVoiceDependencyFailure("Discord bot token is not configured.")

        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        intents.members = True

        self._discord_module = discord
        self._bot_token = bot_token
        self._discord_client = self._build_client(discord=discord, intents=intents)
        self._start_poll_thread()
        self._start_shutdown_thread()
        try:
            self._discord_client.run(bot_token)
        finally:
            self._stop_event.set()
            if self._poll_thread is not None:
                self._poll_thread.join(timeout=5)
            if self._shutdown_thread is not None:
                self._shutdown_thread.join(timeout=5)

    def _build_client(self, *, discord: Any, intents: Any) -> Any:
        service = self

        class _Client(discord.Client):
            async def on_ready(self) -> None:  # type: ignore[override]
                await service._handle_ready()

            async def on_voice_state_update(self, member, before, after) -> None:  # type: ignore[override]
                await service._handle_voice_state_update(member=member, before=before, after=after)

        return _Client(intents=intents)

    def _start_poll_thread(self) -> None:
        if self._poll_thread is not None and self._poll_thread.is_alive():
            return

        def _run() -> None:
            # Bounded poll is used only for silence-based turn finalization.
            while not self._stop_event.is_set():
                with self._runtime_lock:
                    turns = self._runtime.poll()
                for turn in turns:
                    try:
                        self._process_turn(turn=turn)
                    except Exception as exc:  # noqa: BLE001
                        logger.exception("discord_live_voice_turn_failed room_key=%s error=%s", turn.room_key, exc)
                self._stop_event.wait(timeout=_VOICE_POLL_INTERVAL_SECONDS)

        self._poll_thread = threading.Thread(
            target=_run,
            name="discord-live-voice-poll",
            daemon=True,
        )
        self._poll_thread.start()

    def _start_shutdown_thread(self) -> None:
        if self._shutdown_thread is not None and self._shutdown_thread.is_alive():
            return

        def _run() -> None:
            self._stop_event.wait()
            client = self._discord_client
            if client is None:
                return
            loop = getattr(client, "loop", None)
            if loop is None:
                return
            try:
                future = asyncio.run_coroutine_threadsafe(client.close(), loop)
                future.result(timeout=15)
            except Exception:  # noqa: BLE001
                logger.exception("discord_live_voice_shutdown_failed")

        self._shutdown_thread = threading.Thread(
            target=_run,
            name="discord-live-voice-shutdown",
            daemon=True,
        )
        self._shutdown_thread.start()

    async def _handle_ready(self) -> None:
        logger.info("discord_live_voice_ready user_id=%s", getattr(getattr(self._discord_client, "user", None), "id", None))
        self._refresh_room_registry()
        await self._join_occupied_rooms()

    async def _handle_voice_state_update(self, *, member, before, after) -> None:  # noqa: ANN001
        if self._discord_client is None:
            return

        changed_channel_ids = {
            str(getattr(getattr(before, "channel", None), "id", "") or "").strip(),
            str(getattr(getattr(after, "channel", None), "id", "") or "").strip(),
        }
        changed_channel_ids.discard("")
        if not changed_channel_ids:
            return

        self._refresh_room_registry()
        for room in self._rooms_by_key.values():
            if room.voice_channel_id not in changed_channel_ids:
                continue
            voice_channel = self._resolve_voice_channel(room)
            if voice_channel is None:
                continue
            human_count = _human_member_count(voice_channel)
            with self._runtime_lock:
                session = self._runtime.get_session(binding=room.binding)
                if session is None:
                    self._runtime.register_room(
                        binding=room.binding,
                        bot_user_id=_discord_user_id(self._discord_client.user),
                        human_member_count=human_count,
                    )
                    session = self._runtime.get_session(binding=room.binding)
                join_decision = self._runtime.evaluate_join(binding=room.binding, human_member_count=human_count)
                leave_decision = self._runtime.evaluate_leave(binding=room.binding, human_member_count=human_count)

            if join_decision.should_transition:
                await self._join_room(room=room, human_count=human_count)
            elif leave_decision.should_transition:
                await self._leave_room(room=room)

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
                if self._runtime.get_session(binding=room.binding) is None:
                    with self._runtime_lock:
                        self._runtime.register_room(
                            binding=room.binding,
                            bot_user_id=_discord_user_id(getattr(self._discord_client, "user", None)),
                            human_member_count=0,
                        )

    async def _join_occupied_rooms(self) -> None:
        for room in list(self._rooms_by_key.values()):
            voice_channel = self._resolve_voice_channel(room)
            if voice_channel is None:
                continue
            human_count = _human_member_count(voice_channel)
            if human_count <= 0:
                continue
            await self._join_room(room=room, human_count=human_count)

    async def _join_room(self, *, room: ConfiguredLiveVoiceRoom, human_count: int) -> None:
        if self._discord_client is None:
            return
        active_room_key = self._voice_clients_by_guild_id.get(room.guild_id)
        if active_room_key and active_room_key != room.room_key:
            logger.warning(
                "discord_live_voice_room_skipped guild_id=%s room_key=%s active_room_key=%s",
                room.guild_id,
                room.room_key,
                active_room_key,
            )
            return

        if room.room_key in self._voice_clients_by_room_key:
            return

        voice_channel = self._resolve_voice_channel(room)
        if voice_channel is None:
            logger.warning("discord_live_voice_room_missing channel_id=%s guild_id=%s", room.voice_channel_id, room.guild_id)
            return

        voice_client = await voice_channel.connect()
        sink = _build_sink(discord=self._discord_module, service=self, room=room)
        if not hasattr(voice_client, "start_recording"):
            raise DiscordLiveVoiceDependencyFailure("Installed Discord voice client does not support audio recording.")
        voice_client.start_recording(sink, _recording_stopped_callback)
        self._voice_clients_by_room_key[room.room_key] = voice_client
        self._voice_clients_by_guild_id[room.guild_id] = room.room_key
        with self._runtime_lock:
            self._runtime.join_room(binding=room.binding, human_member_count=human_count)
        logger.info("discord_live_voice_joined room_key=%s human_count=%s", room.room_key, human_count)

    async def _leave_room(self, *, room: ConfiguredLiveVoiceRoom) -> None:
        voice_client = self._voice_clients_by_room_key.pop(room.room_key, None)
        self._voice_clients_by_guild_id.pop(room.guild_id, None)
        if voice_client is not None:
            if hasattr(voice_client, "stop_recording"):
                try:
                    voice_client.stop_recording()
                except Exception:  # noqa: BLE001
                    logger.exception("discord_live_voice_stop_recording_failed room_key=%s", room.room_key)
            await voice_client.disconnect(force=True)
        with self._runtime_lock:
            self._runtime.leave_room(binding=room.binding)
        logger.info("discord_live_voice_left room_key=%s", room.room_key)

    def handle_voice_chunk(
        self,
        *,
        room: ConfiguredLiveVoiceRoom,
        user_id: str,
        audio_bytes: bytes,
        sample_rate_hz: int,
        channels: int,
        is_bot_audio: bool,
    ) -> None:
        if not audio_bytes:
            return
        with self._runtime_lock:
            self._runtime.ingest_audio(
                binding=room.binding,
                user_id=user_id,
                audio_bytes=audio_bytes,
                received_at=datetime.now(timezone.utc),
                sample_rate_hz=sample_rate_hz,
                channels=channels,
                is_bot_audio=is_bot_audio,
            )

    def _process_turn(self, *, turn: LiveVoiceTurn) -> None:
        room = self._rooms_by_key.get(turn.room_key)
        if room is None or self._bot_token is None:
            return
        if not self._processing_lock.acquire(blocking=False):
            logger.warning("discord_live_voice_turn_skipped room_key=%s reason=processor_busy", turn.room_key)
            return
        try:
            self._process_turn_locked(room=room, turn=turn)
        finally:
            self._processing_lock.release()

    def _process_turn_locked(self, *, room: ConfiguredLiveVoiceRoom, turn: LiveVoiceTurn) -> None:
        transcript: str | None = None
        with self._session_factory() as session:
            tenant = session.get(Tenant, room.tenant_id)
            if tenant is None or not tenant.is_enabled:
                return
            project = session.get(Project, room.project_id) if room.project_id else resolve_project_for_discord_channel(
                session=session,
                tenant_id=tenant.tenant_id,
                channel_id=room.linked_text_channel_id,
            )

            wav_bytes = _encode_pcm_wav(
                pcm_bytes=turn.audio_bytes,
                sample_rate_hz=turn.sample_rate_hz,
                channels=turn.channels,
            )
            try:
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
                self._post_text_notice(
                    channel_id=room.linked_text_channel_id,
                    content="Live voice transcription returned no text.",
                )
                return

            normalized_issue_key, requested_status, issues, status_counts = collect_ask_context(
                session=session,
                tenant=tenant,
                channel_id=room.linked_text_channel_id,
                question=transcript,
                scoped_issue_key=None,
            )
            project_keys = (
                [str(project.jira_project_key).strip().upper()]
                if project is not None and str(project.jira_project_key).strip()
                else tenant_project_keys(session=session, tenant=tenant)
            )
            codex_working_dir = resolve_codex_working_dir(
                session=session,
                tenant=tenant,
                settings=self._settings,
                project_id=getattr(project, "project_id", None),
                project_keys=project_keys,
            )
            history_owner = project if project is not None else tenant
            github_context = collect_github_ask_context(
                session=session,
                tenant=tenant,
                project_keys=project_keys,
            )
            runtime = build_codex_runtime(session=session, settings=self._settings)

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
                metadata={"finalization_reason": turn.finalization_reason},
            )
            _set_discord_config(target=history_owner, discord_config=history_config)
            updated_history = self._room_history.recent_room_history(
                discord_config=getattr(history_owner, "discord_config", None),
                linked_text_channel_id=room.linked_text_channel_id,
                voice_channel_id=room.voice_channel_id,
            )

            try:
                result = answer_voice_room_turn(
                    runtime=runtime,
                    transcript=transcript,
                    project_keys=project_keys,
                    issues=issues,
                    status_counts=status_counts,
                    invocation_context=CodexInvocationContext(
                        channel="discord",
                        tenant_id=tenant.tenant_id,
                        project_id=getattr(project, "project_id", None),
                        command="pm",
                        stage="live-voice-room",
                        working_dir=codex_working_dir,
                        issue_key=normalized_issue_key,
                    ),
                    history=updated_history,
                    github_context=github_context,
                    tenant_discord_config=getattr(tenant, "discord_config", None) or {},
                    project_discord_config=getattr(project, "discord_config", None) or {},
                )
            except CodexRuntimeError as exc:
                session.commit()
                self._post_text_notice(
                    channel_id=room.linked_text_channel_id,
                    content=f"Live voice persona routing failed: {exc}",
                )
                return

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
                metadata={"router_confidence": result.router_confidence, "router_reason": result.router_reason},
            )
            _set_discord_config(target=history_owner, discord_config=history_config)
            session.commit()

        self._post_text_notice(
            channel_id=room.linked_text_channel_id,
            content=f"<@{turn.user_id}> {transcript}",
        )
        self._post_text_notice(
            channel_id=room.linked_text_channel_id,
            content=f"**{result.persona_name}:** {result.message}",
        )
        self._play_persona_reply(room=room, result=result)

    def _play_persona_reply(self, *, room: ConfiguredLiveVoiceRoom, result) -> None:  # noqa: ANN001
        voice_client = self._voice_clients_by_room_key.get(room.room_key)
        if voice_client is None or self._discord_client is None:
            self._post_text_notice(
                channel_id=room.linked_text_channel_id,
                content="Live voice reply failed: the bot is not connected to the voice room.",
            )
            return
        try:
            audio = synthesize_reply_audio(
                settings=self._settings,
                text=result.message,
                persona_id=result.persona_id,
                room_config=result.room_config,
            )
        except VoiceReplyError as exc:
            self._post_text_notice(
                channel_id=room.linked_text_channel_id,
                content=f"Live voice reply failed for {result.persona_name}: {exc}",
            )
            return

        pcm_bytes = _wav_bytes_to_discord_pcm(audio.audio_bytes)
        source = _DiscordPCMSource(pcm_bytes=pcm_bytes)
        finished = threading.Event()
        playback_error: list[str] = []

        def _after_playback(exc: Exception | None) -> None:
            if exc is not None:
                playback_error.append(str(exc))
            with self._runtime_lock:
                self._runtime.mark_bot_speaking(binding=room.binding, speaking=False)
            finished.set()

        def _start_playback() -> None:
            try:
                with self._runtime_lock:
                    self._runtime.mark_bot_speaking(binding=room.binding, speaking=True)
                voice_client.play(source, after=_after_playback)
            except Exception as exc:  # noqa: BLE001
                playback_error.append(str(exc))
                with self._runtime_lock:
                    self._runtime.mark_bot_speaking(binding=room.binding, speaking=False)
                finished.set()

        self._discord_client.loop.call_soon_threadsafe(_start_playback)
        if not finished.wait(timeout=120):
            playback_error.append("timed out waiting for playback to finish")
            with self._runtime_lock:
                self._runtime.mark_bot_speaking(binding=room.binding, speaking=False)
        if playback_error:
            self._post_text_notice(
                channel_id=room.linked_text_channel_id,
                content=f"Live voice playback failed: {playback_error[-1]}",
            )

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

    def _resolve_voice_channel(self, room: ConfiguredLiveVoiceRoom):  # noqa: ANN001
        if self._discord_client is None:
            return None
        guild = self._discord_client.get_guild(int(room.guild_id))
        if guild is None:
            return None
        return guild.get_channel(int(room.voice_channel_id))

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


def _discord_user_id(user: object) -> str | None:
    normalized = str(getattr(user, "id", "") or "").strip()
    return normalized or None


def _tenant_guild_id(*, tenant: Tenant, settings: Settings) -> str | None:
    tenant_discord_config = getattr(tenant, "discord_config", None) or {}
    guild_id = str(tenant_discord_config.get("guild_id") or "").strip()
    if guild_id:
        return guild_id
    fallback = str(getattr(settings, "discord_guild_id", "") or "").strip()
    return fallback or None


def _set_discord_config(*, target: Tenant | Project, discord_config: dict[str, Any]) -> None:
    target.discord_config = dict(discord_config)
    if hasattr(target, "updated_at"):
        target.updated_at = datetime.now(timezone.utc)


def _human_member_count(channel) -> int:  # noqa: ANN001
    members = getattr(channel, "members", []) or []
    return sum(1 for member in members if not bool(getattr(member, "bot", False)))


def _recording_stopped_callback(*_args, **_kwargs) -> None:
    return None


def _extract_pcm_bytes(voice_data: object) -> bytes:
    if isinstance(voice_data, (bytes, bytearray)):
        return bytes(voice_data)
    for attribute_name in ("pcm", "decoded_data", "audio"):
        value = getattr(voice_data, attribute_name, None)
        if isinstance(value, (bytes, bytearray)):
            return bytes(value)
    return b""


def _load_discord_runtime() -> Any:
    try:
        return import_module("discord")
    except ModuleNotFoundError as exc:
        raise DiscordLiveVoiceDependencyFailure(
            "Live voice requires py-cord with voice support. Install it with `pip install 'py-cord[voice]'`."
        ) from exc


def _build_sink(*, discord: Any, service: DiscordLiveVoiceService, room: ConfiguredLiveVoiceRoom):
    sink_base = getattr(getattr(discord, "sinks", None), "Sink", None)
    if sink_base is None:
        raise DiscordLiveVoiceDependencyFailure("Installed py-cord package does not expose discord.sinks.Sink.")

    class _RoomSink(sink_base):
        def write(self, data, user) -> None:  # noqa: ANN001
            pcm_bytes = _extract_pcm_bytes(data)
            if not pcm_bytes:
                return
            service.handle_voice_chunk(
                room=room,
                user_id=str(getattr(user, "id", "") or "").strip(),
                audio_bytes=pcm_bytes,
                sample_rate_hz=int(getattr(data, "sample_rate", 48_000) or 48_000),
                channels=int(getattr(data, "channels", 2) or 2),
                is_bot_audio=bool(getattr(user, "bot", False)),
            )

    return _RoomSink()


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


def _wav_bytes_to_discord_pcm(wav_bytes: bytes) -> bytes:
    try:
        import numpy
        from scipy import signal
    except ModuleNotFoundError as exc:
        raise DiscordLiveVoiceDependencyFailure(
            "Live voice playback requires numpy and scipy for PCM conversion."
        ) from exc

    with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
        channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()
        sample_rate = wav_file.getframerate()
        pcm_bytes = wav_file.readframes(wav_file.getnframes())
    if sample_width != 2:
        raise DiscordLiveVoiceDependencyFailure("Live voice playback requires 16-bit PCM audio.")
    if channels not in {1, 2}:
        raise DiscordLiveVoiceDependencyFailure("Live voice playback supports mono or stereo WAV audio only.")

    pcm_array = numpy.frombuffer(pcm_bytes, dtype=numpy.int16)
    if pcm_array.size == 0:
        return b""
    pcm_array = pcm_array.reshape(-1, channels)
    if channels == 1:
        pcm_array = numpy.repeat(pcm_array, 2, axis=1)

    if sample_rate != 48_000:
        target_samples = max(1, round(pcm_array.shape[0] * 48_000 / sample_rate))
        pcm_array = signal.resample(pcm_array.astype(numpy.float32), target_samples, axis=0)

    pcm_array = numpy.clip(numpy.rint(pcm_array), -32768, 32767).astype(numpy.int16, copy=False)
    return pcm_array.reshape(-1).tobytes()
