from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import threading
from time import monotonic
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from orchestrator.core.config import Settings
from orchestrator.core.discord.live_voice_audio import encode_wav_to_opus_frames
from orchestrator.core.discord.live_voice_session import LiveVoiceRoomBinding


class LiveVoiceTransportClientError(RuntimeError):
    pass


DEFAULT_TRANSPORT_SESSION_ID = "discord-live-voice"
_TRANSPORT_LOGGER = logging.getLogger("orchestrator.discord_live_voice_transport")


@dataclass(frozen=True)
class LiveVoiceTransportRoom:
    guild_id: str
    channel_id: str


class GoJsonLinesLiveVoiceTransportClient:
    def __init__(
        self,
        *,
        command: Sequence[str],
        bot_token: str | None = None,
        startup_timeout_seconds: int = 10,
        event_handler: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._command = tuple(command)
        self._bot_token = str(bot_token or "").strip() or None
        self._startup_timeout_seconds = max(1, int(startup_timeout_seconds))
        self._event_handler = event_handler
        self._process: subprocess.Popen[str] | None = None
        self._stdin = None
        self._stdout = None
        self._stderr = None
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._monitor_thread: threading.Thread | None = None
        self._ready_event = threading.Event()
        self._startup_state_event = threading.Event()
        self._stop_event = threading.Event()
        self._write_lock = threading.Lock()
        self._process_lock = threading.Lock()
        self._active_session_id: str | None = None
        self._active_signature: tuple[tuple[str, str], ...] | None = None
        self._startup_failure_message: str | None = None

    @property
    def command(self) -> tuple[str, ...]:
        return self._command

    def set_event_handler(self, handler: Callable[[dict[str, Any]], None] | None) -> None:
        self._event_handler = handler

    def start(self) -> None:
        with self._process_lock:
            process = self._process
            if process is not None and process.poll() is None:
                if self._ready_event.is_set() or (self._reader_thread is None and self._monitor_thread is None):
                    return
                self._wait_for_ready()
                return
            if process is not None and process.poll() is not None:
                self._clear_process_state()

        self._stop_event.clear()
        self._ready_event.clear()
        self._startup_state_event.clear()
        self._startup_failure_message = None
        env = dict(os.environ)
        if self._bot_token:
            env["ORCHESTRATOR_DISCORD_BOT_TOKEN"] = self._bot_token

        process = subprocess.Popen(  # noqa: S603
            self._command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env=env,
        )
        if process.stdin is None or process.stdout is None or process.stderr is None:
            process.kill()
            raise LiveVoiceTransportClientError("Live voice transport pipes are unavailable")

        self._process = process
        self._stdin = process.stdin
        self._stdout = process.stdout
        self._stderr = process.stderr
        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            name="discord-live-voice-transport-reader",
            daemon=True,
        )
        self._reader_thread.start()
        self._stderr_thread = threading.Thread(
            target=self._stderr_loop,
            name="discord-live-voice-transport-stderr",
            daemon=True,
        )
        self._stderr_thread.start()
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            args=(process,),
            name="discord-live-voice-transport-monitor",
            daemon=True,
        )
        self._monitor_thread.start()
        self._wait_for_ready()

    def close(self) -> None:
        self._stop_event.set()
        process = self._process
        if process is None:
            return

        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
        finally:
            with self._process_lock:
                self._clear_process_state()

    def sync_session(
        self,
        *,
        session_id: str,
        bot_token: str,
        rooms: Sequence[LiveVoiceTransportRoom],
    ) -> None:
        normalized_session_id = str(session_id or "").strip() or DEFAULT_TRANSPORT_SESSION_ID
        normalized_token = str(bot_token or "").strip()
        if not normalized_token:
            raise LiveVoiceTransportClientError("Live voice transport session requires a Discord bot token.")

        room_payload = [
            {
                "guild_id": str(room.guild_id).strip(),
                "channel_id": str(room.channel_id).strip(),
            }
            for room in rooms
            if str(room.guild_id).strip() and str(room.channel_id).strip()
        ]
        signature = tuple(sorted((room["guild_id"], room["channel_id"]) for room in room_payload))

        if not room_payload:
            self.close_session(session_id=normalized_session_id, reason="no_rooms_configured")
            return
        if self._active_session_id == normalized_session_id and self._active_signature == signature:
            return

        if self._active_session_id is not None:
            self.close_session(session_id=self._active_session_id, reason="session_replaced")

        self._send_frame(
            frame_type="open_session",
            payload={
                "session_id": normalized_session_id,
                "token": normalized_token,
                "rooms": room_payload,
                "auto_join": True,
                "receive_opus": True,
                "send_audio": True,
            },
        )
        self._active_session_id = normalized_session_id
        self._active_signature = signature

    def close_session(self, *, session_id: str | None = None, reason: str = "") -> None:
        normalized_session_id = str(session_id or self._active_session_id or "").strip()
        if not normalized_session_id:
            return
        self._send_frame(
            frame_type="close_session",
            payload={
                "session_id": normalized_session_id,
                "reason": reason,
            },
        )
        if normalized_session_id == self._active_session_id:
            self._active_session_id = None
            self._active_signature = None

    def play_audio(
        self,
        *,
        binding: LiveVoiceRoomBinding,
        audio_bytes: bytes,
        content_type: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if str(content_type or "").strip().lower() != "audio/wav":
            raise LiveVoiceTransportClientError(
                f"Live voice transport only supports WAV reply input, received {content_type!r}."
            )
        session_id = str(self._active_session_id or DEFAULT_TRANSPORT_SESSION_ID).strip()
        opus_frames = encode_wav_to_opus_frames(wav_bytes=audio_bytes)
        if not opus_frames:
            raise LiveVoiceTransportClientError("Live voice transport could not encode any reply Opus frames.")
        self._send_frame(
            frame_type="play_audio",
            payload={
                "session_id": session_id,
                "room": {
                    "guild_id": str(binding.guild_id).strip(),
                    "channel_id": str(binding.voice_channel_id).strip(),
                },
                "opus_frames_base64": [_encode_base64(frame) for frame in opus_frames],
            },
        )

    def stop_audio(
        self,
        *,
        binding: LiveVoiceRoomBinding,
    ) -> None:
        session_id = str(self._active_session_id or DEFAULT_TRANSPORT_SESSION_ID).strip()
        self._send_frame(
            frame_type="stop_audio",
            payload={
                "session_id": session_id,
                "room": {
                    "guild_id": str(binding.guild_id).strip(),
                    "channel_id": str(binding.voice_channel_id).strip(),
                },
            },
        )

    def _reader_loop(self) -> None:
        stdout = self._stdout
        if stdout is None:
            self._mark_startup_failed("Live voice transport stdout pipe is unavailable.")
            return

        for raw_line in stdout:
            line = raw_line.strip()
            if not line:
                continue
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                continue

            frame_type = str(frame.get("type") or "").strip()
            if frame_type == "transport_ready":
                self._ready_event.set()
                self._startup_state_event.set()

            handler = self._event_handler
            if handler is not None:
                handler(dict(frame))

    def _stderr_loop(self) -> None:
        stderr = self._stderr
        if stderr is None:
            return
        for raw_line in stderr:
            line = raw_line.strip()
            if line:
                _TRANSPORT_LOGGER.info("%s", line)

    def _monitor_loop(self, process: subprocess.Popen[str]) -> None:
        return_code = process.wait()
        active_session_id = DEFAULT_TRANSPORT_SESSION_ID
        should_emit_failure = False
        with self._process_lock:
            if self._active_session_id:
                active_session_id = self._active_session_id
            if self._process is process:
                self._clear_process_state()
                should_emit_failure = True
        if self._stop_event.is_set():
            return
        if not self._ready_event.is_set():
            self._mark_startup_failed(f"Live voice transport process exited with code {return_code} before readiness.")
        if not should_emit_failure:
            return
        _TRANSPORT_LOGGER.warning("live_voice_transport_process_exited return_code=%s", return_code)
        handler = self._event_handler
        if handler is not None:
            handler(
                {
                    "type": "transport_failed",
                    "session_id": active_session_id,
                    "stage": "process_exit",
                    "error": f"Live voice transport process exited with code {return_code}",
                    "retryable": True,
                }
            )

    def _send_frame(self, *, frame_type: str, payload: dict[str, Any]) -> None:
        self.start()
        stdin = self._stdin
        process = self._process
        if stdin is None or process is None or process.poll() is not None:
            raise LiveVoiceTransportClientError("Live voice transport is not running")

        frame = {"type": frame_type, **payload}
        with self._write_lock:
            stdin.write(json.dumps(frame, separators=(",", ":")) + "\n")
            stdin.flush()

    def _clear_process_state(self) -> None:
        self._process = None
        self._stdin = None
        self._stdout = None
        self._stderr = None
        self._active_session_id = None
        self._active_signature = None

    def _wait_for_ready(self) -> None:
        deadline = monotonic() + float(self._startup_timeout_seconds)
        while True:
            if self._ready_event.is_set():
                return
            if self._startup_failure_message:
                raise LiveVoiceTransportClientError(self._startup_failure_message)
            remaining = deadline - monotonic()
            if remaining <= 0:
                self.close()
                raise LiveVoiceTransportClientError(
                    f"Live voice transport did not become ready within {self._startup_timeout_seconds} seconds."
                )
            self._startup_state_event.wait(timeout=remaining)
            self._startup_state_event.clear()

    def _mark_startup_failed(self, message: str) -> None:
        if self._ready_event.is_set():
            return
        if not self._startup_failure_message:
            self._startup_failure_message = message
        self._startup_state_event.set()


def build_live_voice_transport_client(
    *,
    settings: Settings,
    bot_token: str | None = None,
    event_handler: Callable[[dict[str, Any]], None] | None = None,
) -> GoJsonLinesLiveVoiceTransportClient:
    command_text = str(getattr(settings, "discord_live_voice_transport_command", "") or "").strip()
    if not command_text:
        raise LiveVoiceTransportClientError(
            "Live voice requires ORCHESTRATOR_LIVE_VOICE_TRANSPORT_COMMAND to launch the Go transport."
        )

    startup_timeout_seconds = int(
        getattr(settings, "discord_live_voice_transport_startup_timeout_seconds", 10) or 10
    )
    command = shlex.split(command_text)
    return GoJsonLinesLiveVoiceTransportClient(
        command=command,
        bot_token=bot_token,
        startup_timeout_seconds=startup_timeout_seconds,
        event_handler=event_handler,
    )


JsonLinesLiveVoiceTransportClient = GoJsonLinesLiveVoiceTransportClient


def _encode_base64(data: bytes) -> str:
    import base64

    return base64.b64encode(bytes(data)).decode("ascii")


def _stringify_mapping(mapping: dict[str, Any]) -> dict[str, str]:
    return {str(key): "" if value is None else str(value) for key, value in mapping.items()}
