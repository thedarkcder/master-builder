from __future__ import annotations

import threading
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pytest

from orchestrator.api.schemas import DiscordCommandResponse
from orchestrator.core.discord.live_voice_service import DiscordLiveVoiceService
from orchestrator.core.discord.live_voice_session import LiveVoiceTurn
from orchestrator.core.runtime.payload_models import VoiceEntryRoute
from tests.production_path_support import (
    FakeDiscordApiClient,
    clear_runtime_environment,
    configure_runtime_environment,
    seed_core_runtime_state,
    session_factory_for,
)

pytestmark = pytest.mark.production_path


class _FakeSidecarClient:
    def __init__(self) -> None:
        self.synced_rooms: list[dict[str, object]] = []
        self.play_audio_calls: list[dict[str, object]] = []
        self.playback_event = threading.Event()

    def start(self) -> None:
        return None

    def close(self) -> None:
        return None

    def set_event_handler(self, handler) -> None:  # noqa: ANN001
        self._handler = handler

    def sync_session(self, *, session_id: str, bot_token: str, rooms) -> None:  # noqa: ANN001
        self.synced_rooms.append(
            {
                "session_id": session_id,
                "bot_token": bot_token,
                "rooms": list(rooms),
            }
        )

    def play_audio(self, *, binding, audio_bytes, content_type, metadata=None) -> None:  # noqa: ANN001
        self.play_audio_calls.append(
            {
                "binding": binding,
                "audio_bytes": bytes(audio_bytes),
                "content_type": content_type,
                "metadata": dict(metadata or {}),
            }
        )
        self.playback_event.set()


class LiveVoiceProductionPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.database_url, _ = configure_runtime_environment(
            temp_dir=self.temp_dir,
            database_name="live_voice_production.db",
        )
        self.session_factory = session_factory_for(self.database_url)
        seed_core_runtime_state(
            self.session_factory,
            tenant_discord_config={
                "guild_id": "guild-1",
                "live_voice_enabled": True,
                "live_voice_room_links": {"voice-room-1": "text-room-1"},
            },
            project_discord_config={
                "channel_id": "discord-channel-1",
                "notify_events": [],
                "allowed_user_ids": ["u-1"],
                "live_voice_enabled": True,
                "live_voice_room_links": {"voice-room-1": "text-room-1"},
            },
        )
        self.discord_client = FakeDiscordApiClient(bot_token="discord-token")
        self.sidecar = _FakeSidecarClient()
        self.service = DiscordLiveVoiceService(
            settings=SimpleNamespace(
                discord_guild_id="",
                secrets_encryption_key="enc",
                codex_model="gpt-5",
                voice_stt_provider="openai",
                voice_tts_provider="pocket_tts",
            ),
            session_factory=self.session_factory,
            secret_resolver=lambda *args, **kwargs: "discord-token",
            discord_api_client_factory=lambda bot_token: self.discord_client,
            transport_client_factory=lambda **kwargs: self.sidecar,
        )
        self.service._transport_client = self.sidecar
        self.service._bot_token = "discord-token"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        clear_runtime_environment()

    def test_refresh_room_registry_and_sync_uses_real_project_config(self) -> None:
        self.service._refresh_room_registry()
        self.service._sync_rooms_with_sidecar()

        self.assertIn("guild-1:voice-room-1", self.service._rooms_by_key)
        self.assertEqual(len(self.sidecar.synced_rooms), 1)
        synced = self.sidecar.synced_rooms[0]
        self.assertEqual(synced["bot_token"], "discord-token")
        self.assertEqual(len(synced["rooms"]), 1)
        self.assertEqual(synced["rooms"][0].guild_id, "guild-1")
        self.assertEqual(synced["rooms"][0].channel_id, "voice-room-1")

    def test_process_turn_runs_real_service_flow_and_dispatches_playback(self) -> None:
        self.service._refresh_room_registry()
        room = self.service._rooms_by_key["guild-1:voice-room-1"]
        self.service._turn_versions[room.room_key] = 1
        turn = LiveVoiceTurn(
            binding=room.binding,
            turn_index=1,
            user_id="u-1",
            audio_bytes=b"\x00\x01\x02\x03",
            started_at=datetime.now(timezone.utc),
            ended_at=datetime.now(timezone.utc),
            sample_rate_hz=48_000,
            channels=2,
            finalization_reason="test",
        )
        def _fake_execute(**_kwargs):  # noqa: ANN003
            return DiscordCommandResponse(
                ok=True,
                command="ask",
                message="Reject relink and keep the device bound to the original user.",
                data={"persona_id": "pm", "brief": {}},
            )

        with (
            patch("orchestrator.core.discord.live_voice_service.resolve_codex_working_dir", return_value=self.temp_dir.name),
            patch("orchestrator.core.discord.live_voice_service.transcribe_audio_bytes", return_value="What is the relink policy?"),
            patch(
                "orchestrator.core.discord.live_voice_service.route_discord_voice_entry",
                return_value=VoiceEntryRoute(lane="ask", persona="pm", confidence=0.91, reason="policy"),
            ),
            patch(
                "orchestrator.core.discord.live_voice_service.execute_tenant_command_ingress",
                side_effect=_fake_execute,
            ),
            patch(
                "orchestrator.core.discord.live_voice_service.synthesize_reply_audio",
                return_value=SimpleNamespace(
                    filename="reply.wav",
                    audio_bytes=b"wav-bytes",
                    content_type="audio/wav",
                ),
            ),
        ):
            self.service._process_turn(turn=turn, turn_version=1)
            self.assertTrue(self.sidecar.playback_event.wait(timeout=5.0))

        self.assertEqual(
            len(self.discord_client.posted_messages),
            0,
            "Voice room success path posts no transcript/answer text; reply is audio in VC only.",
        )
        self.assertEqual(len(self.sidecar.play_audio_calls), 1)
        self.assertEqual(self.sidecar.play_audio_calls[0]["content_type"], "audio/wav")
        self.assertEqual(self.sidecar.play_audio_calls[0]["metadata"]["persona_id"], "pm")
