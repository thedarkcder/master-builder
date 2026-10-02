from __future__ import annotations

import base64
import threading
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from orchestrator.api.schemas import DiscordCommandResponse
from orchestrator.core.discord.live_voice_service import (
    ConfiguredLiveVoiceRoom,
    DiscordLiveVoiceService,
    _encode_pcm_wav,
)
from orchestrator.core.discord.live_voice_session import LiveVoiceTurn
from orchestrator.core.observability.otel import current_log_context
from orchestrator.core.runtime.payload_models import VoiceEntryRoute


class _FakeSidecarClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def start(self) -> None:
        self.calls.append(("start", (), {}))

    def close(self) -> None:
        self.calls.append(("close", (), {}))

    def set_event_handler(self, handler) -> None:  # noqa: ANN001
        self.calls.append(("set_event_handler", (), {}))
        self._event_handler = handler

    def sync_session(self, *, session_id: str, bot_token: str, rooms) -> None:  # noqa: ANN001
        self.calls.append(
            ("sync_session", (session_id, bot_token), {"rooms": list(rooms)})
        )

    def close_session(self, *, session_id: str | None = None, reason: str = "") -> None:
        self.calls.append(("close_session", (session_id, reason), {}))

    def play_audio(self, *, binding, audio_bytes, content_type, metadata=None) -> None:  # noqa: ANN001
        self.calls.append(
            (
                "play_audio",
                (binding.room_key, bytes(audio_bytes), content_type),
                {"metadata": metadata or {}},
            )
        )

    def stop_audio(self, *, binding) -> None:  # noqa: ANN001
        self.calls.append(("stop_audio", (binding.room_key,), {}))


class LiveVoiceServiceTests(unittest.TestCase):
    def test_encode_pcm_wav_wraps_pcm_payload(self) -> None:
        wav_bytes = _encode_pcm_wav(
            pcm_bytes=b"\x00\x01\x02\x03", sample_rate_hz=48_000, channels=2
        )

        self.assertTrue(wav_bytes.startswith(b"RIFF"))
        self.assertIn(b"WAVE", wav_bytes)

    def test_sync_rooms_opens_new_sessions_and_closes_removed_sessions(self) -> None:
        service = DiscordLiveVoiceService(
            settings=SimpleNamespace(), session_factory=lambda: None
        )
        service._transport_client = _FakeSidecarClient()
        service._bot_token = "bot-token"
        room = ConfiguredLiveVoiceRoom(
            tenant_id="tenant-a",
            guild_id="123",
            voice_channel_id="456",
            linked_text_channel_id="789",
            project_id="project-a",
        )
        service._rooms_by_key = {room.room_key: room}

        service._sync_rooms_with_sidecar()
        sync_call = next(
            call
            for call in service._transport_client.calls
            if call[0] == "sync_session"
        )
        self.assertEqual(sync_call[1], ("discord-live-voice", "bot-token"))
        self.assertEqual(len(sync_call[2]["rooms"]), 1)
        self.assertIn(room.room_key, service._synced_room_keys)

        service._rooms_by_key = {}
        service._sync_rooms_with_sidecar()
        self.assertEqual(
            service._transport_client.calls[-1],
            ("sync_session", ("discord-live-voice", "bot-token"), {"rooms": []}),
        )

    def test_opus_frame_event_runs_through_python_turn_processing(self) -> None:
        service = DiscordLiveVoiceService(
            settings=SimpleNamespace(), session_factory=lambda: None
        )
        service._transport_client = _FakeSidecarClient()
        service._bot_token = "bot-token"
        room = ConfiguredLiveVoiceRoom(
            tenant_id="tenant-a",
            guild_id="123",
            voice_channel_id="456",
            linked_text_channel_id="789",
            project_id="project-a",
        )
        service._rooms_by_key = {room.room_key: room}
        service._handle_transport_event(
            {
                "type": "transport_ready",
                "session_id": "discord-live-voice",
                "backend": "go",
            }
        )
        service._handle_transport_event(
            {
                "type": "room_state",
                "session_id": "discord-live-voice",
                "guild_id": room.guild_id,
                "channel_id": room.voice_channel_id,
                "human_count": 1,
                "joined": True,
            }
        )

        processed_turns: list[bytes] = []
        finalized_turn = LiveVoiceTurn(
            binding=room.binding,
            turn_index=1,
            user_id="user-1",
            audio_bytes=b"pcm-audio",
            started_at=datetime.now(timezone.utc),
            ended_at=datetime.now(timezone.utc),
            sample_rate_hz=48_000,
            channels=2,
            finalization_reason="test",
        )

        def _decode(
            _room_key: str, _user_id: str, _packets: list[bytes]
        ) -> tuple[bytes, int, int]:
            return b"pcm-audio", 48_000, 2

        def _process_turn(*, turn, turn_version) -> None:  # noqa: ANN001
            _ = turn_version
            processed_turns.append(turn.audio_bytes)

        service._decode_member_speech = _decode
        service._process_turn = _process_turn
        service._runtime.ingest_audio = lambda **_: finalized_turn  # type: ignore[method-assign]
        service._start_turn_processing = (  # type: ignore[method-assign]
            lambda *, turn: service._process_turn(turn=turn, turn_version=1)
        )

        service._handle_transport_event(
            {
                "type": "opus_frame",
                "session_id": "discord-live-voice",
                "guild_id": room.guild_id,
                "channel_id": room.voice_channel_id,
                "user_id": "user-1",
                "opus_frame_base64": base64.b64encode(b"opus-a").decode("ascii"),
            }
        )

        self.assertEqual(processed_turns, [b"pcm-audio"])

    def test_opus_frame_event_ignores_unknown_user_frames(self) -> None:
        service = DiscordLiveVoiceService(
            settings=SimpleNamespace(), session_factory=lambda: None
        )
        room = ConfiguredLiveVoiceRoom(
            tenant_id="tenant-a",
            guild_id="123",
            voice_channel_id="456",
            linked_text_channel_id="789",
            project_id="project-a",
        )
        service._rooms_by_key = {room.room_key: room}

        decoded_packets: list[bytes] = []

        def _decode(
            _room_key: str, _user_id: str, packets: list[bytes]
        ) -> tuple[bytes, int, int]:
            decoded_packets.extend(packets)
            return b"", 48_000, 2

        service._decode_member_speech = _decode  # type: ignore[method-assign]

        service._handle_transport_event(
            {
                "type": "opus_frame",
                "session_id": "discord-live-voice",
                "guild_id": room.guild_id,
                "channel_id": room.voice_channel_id,
                "user_id": "0",
                "opus_frame_base64": base64.b64encode(b"opus-a").decode("ascii"),
            }
        )

        self.assertEqual(decoded_packets, [])

    def test_decode_member_speech_resets_decoder_after_corrupt_stream(self) -> None:
        service = DiscordLiveVoiceService(
            settings=SimpleNamespace(), session_factory=lambda: None
        )

        class _BrokenDecoder:
            def decode(self, _packet: bytes) -> bytes:
                raise RuntimeError("corrupted stream")

        service._decoder_by_key["room-a:user-a"] = _BrokenDecoder()

        with self.assertRaisesRegex(Exception, "corrupt Opus frame"):
            service._decode_member_speech("room-a", "user-a", [b"opus"])

        self.assertNotIn("room-a:user-a", service._decoder_by_key)

    def test_start_turn_processing_keeps_only_latest_pending_turn_for_room(
        self,
    ) -> None:
        service = DiscordLiveVoiceService(
            settings=SimpleNamespace(), session_factory=lambda: None
        )
        processed: list[int] = []
        first_turn_started = threading.Event()
        worker_release = threading.Event()
        worker_finished = threading.Event()

        turn_one = LiveVoiceTurn(
            binding=ConfiguredLiveVoiceRoom(
                tenant_id="tenant-a",
                guild_id="123",
                voice_channel_id="456",
                linked_text_channel_id="789",
                project_id="project-a",
            ).binding,
            turn_index=1,
            user_id="user-1",
            audio_bytes=b"pcm-audio-1",
            started_at=datetime.now(timezone.utc),
            ended_at=datetime.now(timezone.utc),
            sample_rate_hz=48_000,
            channels=2,
            finalization_reason="test",
        )
        turn_two = LiveVoiceTurn(
            binding=turn_one.binding,
            turn_index=2,
            user_id="user-1",
            audio_bytes=b"pcm-audio-2",
            started_at=datetime.now(timezone.utc),
            ended_at=datetime.now(timezone.utc),
            sample_rate_hz=48_000,
            channels=2,
            finalization_reason="test",
        )
        turn_three = LiveVoiceTurn(
            binding=turn_one.binding,
            turn_index=3,
            user_id="user-1",
            audio_bytes=b"pcm-audio-3",
            started_at=datetime.now(timezone.utc),
            ended_at=datetime.now(timezone.utc),
            sample_rate_hz=48_000,
            channels=2,
            finalization_reason="test",
        )

        def _process_turn(*, turn, turn_version) -> None:  # noqa: ANN001
            _ = turn_version
            processed.append(turn.turn_index)
            if turn.turn_index == 1:
                first_turn_started.set()
                self.assertTrue(worker_release.wait(timeout=2))
                return
            worker_finished.set()

        service._process_turn = _process_turn  # type: ignore[method-assign]

        service._start_turn_processing(turn=turn_one)
        self.assertTrue(first_turn_started.wait(timeout=2))
        service._start_turn_processing(turn=turn_two)
        service._start_turn_processing(turn=turn_three)
        worker_release.set()

        self.assertTrue(worker_finished.wait(timeout=2))
        self.assertEqual(processed, [1, 3])

    def test_process_turn_locked_uses_fast_live_voice_profile(self) -> None:
        tenant = SimpleNamespace(
            tenant_id="tenant-a",
            is_enabled=True,
            discord_config={},
        )
        project = SimpleNamespace(
            project_id="project-a",
            tenant_id="tenant-a",
            jira_project_key="MAB",
            discord_config={},
        )

        class _FakeSession:
            def __init__(self) -> None:
                self.committed = False

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb) -> bool:
                return False

            def get(self, model, key):  # noqa: ANN001
                model_name = getattr(model, "__name__", "")
                if model_name == "Tenant" and key == tenant.tenant_id:
                    return tenant
                if model_name == "Project" and key == project.project_id:
                    return project
                return None

            def commit(self) -> None:
                self.committed = True

            def rollback(self) -> None:
                pass

            def refresh(self, _obj) -> None:  # noqa: ANN001
                return None

        fake_session = _FakeSession()
        service = DiscordLiveVoiceService(
            settings=SimpleNamespace(),
            session_factory=lambda: fake_session,
        )
        service._bot_token = "bot-token"
        room = ConfiguredLiveVoiceRoom(
            tenant_id="tenant-a",
            guild_id="123",
            voice_channel_id="456",
            linked_text_channel_id="789",
            project_id="project-a",
        )
        service._rooms_by_key = {room.room_key: room}
        turn = LiveVoiceTurn(
            binding=room.binding,
            turn_index=1,
            user_id="user-1",
            audio_bytes=b"pcm-audio",
            started_at=datetime.now(timezone.utc),
            ended_at=datetime.now(timezone.utc),
            sample_rate_hz=48_000,
            channels=2,
            finalization_reason="test",
        )
        room_history = [{"index": index} for index in range(12)]
        scheduled_results: list[tuple[ConfiguredLiveVoiceRoom, object, int]] = []
        notices: list[tuple[str, str]] = []
        service._schedule_persona_reply = (  # type: ignore[method-assign]
            lambda *, room, result, turn_version: scheduled_results.append(
                (room, result, turn_version)
            )
        )
        service._post_text_notice = (  # type: ignore[method-assign]
            lambda *, channel_id, content: notices.append((channel_id, content))
        )
        service._room_history.append_room_history_entry = (  # type: ignore[method-assign]
            lambda **kwargs: (dict(kwargs.get("discord_config") or {}), None)
        )
        service._room_history.recent_room_history = (  # type: ignore[method-assign]
            lambda **kwargs: list(room_history)
        )

        ingress_calls: list[dict] = []
        transcription_contexts: list[dict[str, str | None]] = []
        ingress_contexts: list[dict[str, str | None]] = []

        def _execute_tenant_command_ingress(**kwargs):  # noqa: ANN003
            ingress_contexts.append(dict(current_log_context()))
            ingress_calls.append(kwargs)
            return DiscordCommandResponse(
                ok=True,
                command="!ask",
                message="Short answer.",
                data={"persona_id": "pm", "brief": {}},
            )

        def _transcribe_audio_bytes(**_kwargs):  # noqa: ANN003
            transcription_contexts.append(dict(current_log_context()))
            return "What should we do next?"

        with (
            patch(
                "orchestrator.core.discord.live_voice_service._encode_pcm_wav",
                return_value=b"wav",
            ),
            patch(
                "orchestrator.core.discord.live_voice_service.transcribe_audio_bytes",
                side_effect=_transcribe_audio_bytes,
            ),
            patch(
                "orchestrator.core.discord.live_voice_service.DiscordLiveVoiceService._collect_live_voice_context",
                return_value=("MAB-174", None, ["MAB"], "/tmp/test-repo"),
            ),
            patch(
                "orchestrator.core.discord.live_voice_service.route_discord_voice_entry",
                return_value=VoiceEntryRoute(
                    lane="ask", persona="pm", confidence=0.9, reason="product"
                ),
            ),
            patch(
                "orchestrator.core.discord.live_voice_service.execute_tenant_command_ingress",
                side_effect=_execute_tenant_command_ingress,
            ),
        ):
            service._turn_versions[room.room_key] = 1
            service._process_turn_locked(room=room, turn=turn, turn_version=1)

        self.assertTrue(fake_session.committed)
        self.assertEqual(len(ingress_calls), 1)
        ingress_call = ingress_calls[0]
        self.assertEqual(ingress_call["tenant_id"], "tenant-a")
        self.assertEqual(ingress_call["ingress_source"], "discord")
        payload = ingress_call["payload"]
        self.assertEqual(payload.command, "!ask What should we do next?")
        self.assertEqual(
            payload.command_params,
            {
                "room_mode": "true",
                "room_source": "live_voice",
                "linked_text_channel_id": "789",
                "voice_channel_id": "456",
                "persona_id": "pm",
            },
        )
        self.assertEqual(
            transcription_contexts,
            [
                {
                    "correlation_id": "live-voice:123:456:user-1:1:1",
                    "tenant_id": "tenant-a",
                    "project_id": "project-a",
                    "agent_id": None,
                }
            ],
        )
        self.assertEqual(
            ingress_contexts,
            [
                {
                    "correlation_id": "live-voice:123:456:user-1:1:1",
                    "tenant_id": "tenant-a",
                    "project_id": "project-a",
                    "agent_id": None,
                }
            ],
        )
        self.assertEqual(len(scheduled_results), 1)
        self.assertEqual(scheduled_results[0][2], 1)
        self.assertEqual(scheduled_results[0][1].persona_id, "pm")
        self.assertEqual(scheduled_results[0][1].message, "Short answer.")
        self.assertEqual(notices, [])

    def test_opus_frame_interrupts_active_playback_before_ingesting_user_audio(
        self,
    ) -> None:
        service = DiscordLiveVoiceService(
            settings=SimpleNamespace(), session_factory=lambda: None
        )
        service._transport_client = _FakeSidecarClient()
        room = ConfiguredLiveVoiceRoom(
            tenant_id="tenant-a",
            guild_id="123",
            voice_channel_id="456",
            linked_text_channel_id="789",
            project_id="project-a",
        )
        service._rooms_by_key = {room.room_key: room}
        service._handle_transport_event(
            {
                "type": "room_state",
                "session_id": "discord-live-voice",
                "guild_id": room.guild_id,
                "channel_id": room.voice_channel_id,
                "human_count": 1,
                "joined": True,
            }
        )
        service._runtime.mark_bot_speaking(binding=room.binding, speaking=True)
        service._decode_member_speech = lambda *_args: (b"pcm", 48_000, 2)  # type: ignore[method-assign]
        service._runtime.ingest_audio = lambda **_: None  # type: ignore[method-assign]

        service._handle_transport_event(
            {
                "type": "opus_frame",
                "session_id": "discord-live-voice",
                "guild_id": room.guild_id,
                "channel_id": room.voice_channel_id,
                "user_id": "user-1",
                "opus_frame_base64": base64.b64encode(b"opus-a").decode("ascii"),
            }
        )

        self.assertIn(("stop_audio", ("123:456",), {}), service._transport_client.calls)
        self.assertFalse(
            service._runtime.get_session(binding=room.binding).bot_speaking
        )

    def test_retryable_transport_failure_keeps_transport_ready(self) -> None:
        service = DiscordLiveVoiceService(
            settings=SimpleNamespace(), session_factory=lambda: None
        )

        service._handle_transport_event(
            {
                "type": "transport_ready",
                "session_id": "discord-live-voice",
                "backend": "go",
            }
        )
        service._handle_transport_event(
            {
                "type": "transport_failed",
                "session_id": "discord-live-voice",
                "stage": "open_voice_conn",
                "error": "context deadline exceeded",
                "retryable": True,
            }
        )

        self.assertTrue(service._transport_ready)


class LiveVoiceServiceAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_play_persona_reply_keeps_speaking_until_playback_finished(
        self,
    ) -> None:
        service = DiscordLiveVoiceService(
            settings=SimpleNamespace(), session_factory=lambda: None
        )
        service._transport_client = _FakeSidecarClient()
        service._bot_token = "bot-token"
        room = ConfiguredLiveVoiceRoom(
            tenant_id="tenant-a",
            guild_id="123",
            voice_channel_id="456",
            linked_text_channel_id="789",
            project_id="project-a",
        )
        service._rooms_by_key = {room.room_key: room}
        service._handle_transport_event(
            {
                "type": "transport_ready",
                "session_id": "discord-live-voice",
                "backend": "go",
            }
        )
        service._handle_transport_event(
            {
                "type": "room_state",
                "session_id": "discord-live-voice",
                "guild_id": room.guild_id,
                "channel_id": room.voice_channel_id,
                "human_count": 1,
                "joined": True,
            }
        )

        result = SimpleNamespace(
            persona_id="persona-1",
            persona_name="Persona One",
            persona_role="Engineer",
            message="Hello there",
            room_config={},
        )
        tts_contexts: list[dict[str, str | None]] = []

        with patch(
            "orchestrator.core.discord.live_voice_service.synthesize_reply_audio",
            side_effect=lambda **_kwargs: (
                tts_contexts.append(dict(current_log_context()))
                or SimpleNamespace(audio_bytes=b"wav-bytes")
            ),
        ) as synth_mock:
            service._turn_versions[room.room_key] = 1
            await service._play_persona_reply(room=room, result=result, turn_version=1)

        self.assertIn(
            (
                "play_audio",
                ("123:456", b"wav-bytes", "audio/wav"),
                {
                    "metadata": {
                        "persona_id": "persona-1",
                        "persona_name": "Persona One",
                        "room_key": "123:456",
                    }
                },
            ),
            service._transport_client.calls,
        )
        self.assertEqual(
            synth_mock.call_args.kwargs["text"],
            "Persona One from Engineering. Hello there",
        )
        self.assertEqual(
            tts_contexts,
            [
                {
                    "correlation_id": "live-voice-reply:123:456:1:persona-1",
                    "tenant_id": "tenant-a",
                    "project_id": "project-a",
                    "agent_id": None,
                }
            ],
        )
        self.assertTrue(service._runtime.get_session(binding=room.binding).bot_speaking)

        service._handle_transport_event(
            {
                "type": "playback_finished",
                "session_id": "discord-live-voice",
                "guild_id": room.guild_id,
                "channel_id": room.voice_channel_id,
                "playback_frames": 2,
            }
        )
        self.assertFalse(
            service._runtime.get_session(binding=room.binding).bot_speaking
        )


if __name__ == "__main__":
    unittest.main()
