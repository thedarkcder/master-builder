from __future__ import annotations

import base64
import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from orchestrator.core.discord.live_voice_runtime import LiveVoiceTransport
from orchestrator.core.discord.live_voice_session import LiveVoiceRoomBinding
from orchestrator.core.discord.live_voice_transport_client import (
    GoJsonLinesLiveVoiceTransportClient,
    LiveVoiceTransportRoom,
    build_live_voice_transport_client,
)


class LiveVoiceTransportClientTests(unittest.TestCase):
    def test_transport_client_emits_json_lines_frames(self) -> None:
        binding = LiveVoiceRoomBinding(guild_id="guild-1", voice_channel_id="voice-1")
        client = GoJsonLinesLiveVoiceTransportClient(
            command=["/usr/local/bin/live-voice-transport"],
            bot_token="bot-token",
        )
        client._stdin = io.StringIO()
        client._process = SimpleNamespace(poll=lambda: None)

        client.sync_session(
            session_id="sess-1",
            bot_token="bot-token",
            rooms=[LiveVoiceTransportRoom(guild_id="guild-1", channel_id="voice-1")],
        )
        with patch(
            "orchestrator.core.discord.live_voice_transport_client.encode_wav_to_opus_frames",
            return_value=[b"opus-1", b"opus-2"],
        ):
            client.play_audio(
                binding=binding,
                audio_bytes=b"pcm",
                content_type="audio/wav",
                metadata={"persona_id": "persona-1"},
            )
        client.close_session(session_id="sess-1", reason="room_removed")

        frames = [line for line in client._stdin.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(frames), 3)

        open_frame = _decode_frame(frames[0])
        self.assertEqual(open_frame["type"], "open_session")
        self.assertEqual(open_frame["session_id"], "sess-1")
        self.assertEqual(open_frame["token"], "bot-token")
        self.assertEqual(open_frame["rooms"], [{"guild_id": "guild-1", "channel_id": "voice-1"}])

        play_frame = _decode_frame(frames[1])
        self.assertEqual(play_frame["type"], "play_audio")
        self.assertEqual(play_frame["session_id"], "sess-1")
        self.assertEqual(play_frame["room"], {"guild_id": "guild-1", "channel_id": "voice-1"})
        self.assertEqual(
            play_frame["opus_frames_base64"],
            [
                base64.b64encode(b"opus-1").decode("ascii"),
                base64.b64encode(b"opus-2").decode("ascii"),
            ],
        )

        close_frame = _decode_frame(frames[2])
        self.assertEqual(close_frame["type"], "close_session")
        self.assertEqual(close_frame["session_id"], "sess-1")
        self.assertEqual(close_frame["reason"], "room_removed")

    def test_transport_client_emits_stop_audio_frame(self) -> None:
        binding = LiveVoiceRoomBinding(guild_id="guild-1", voice_channel_id="voice-1")
        client = GoJsonLinesLiveVoiceTransportClient(
            command=["/usr/local/bin/live-voice-transport"],
            bot_token="bot-token",
        )
        client._stdin = io.StringIO()
        client._process = SimpleNamespace(poll=lambda: None)
        client._active_session_id = "sess-1"

        client.stop_audio(binding=binding)

        frame = _decode_frame(client._stdin.getvalue().strip())
        self.assertEqual(frame["type"], "stop_audio")
        self.assertEqual(frame["session_id"], "sess-1")
        self.assertEqual(frame["room"], {"guild_id": "guild-1", "channel_id": "voice-1"})

    def test_builder_uses_transport_command_from_settings(self) -> None:
        settings = SimpleNamespace(
            discord_live_voice_transport_command="/usr/local/bin/live-voice-transport --flag",
            discord_live_voice_transport_startup_timeout_seconds=12,
        )

        client = build_live_voice_transport_client(settings=settings, bot_token="bot-token")

        self.assertIsInstance(client, GoJsonLinesLiveVoiceTransportClient)
        self.assertEqual(client.command[:2], ("/usr/local/bin/live-voice-transport", "--flag"))

    def test_transport_protocol_is_preserved_for_structural_fakes(self) -> None:
        class _FakeTransport:
            def sync_session(self, *, session_id: str, bot_token: str, rooms) -> None:  # noqa: ANN001
                pass

            def close_session(self, *, session_id: str | None = None, reason: str = "") -> None:
                pass

            def play_audio(self, *, binding, audio_bytes, content_type, metadata=None) -> None:  # noqa: ANN001
                pass

            def stop_audio(self, *, binding) -> None:  # noqa: ANN001
                pass

        self.assertIsInstance(_FakeTransport(), LiveVoiceTransport)


def _decode_frame(line: str) -> dict:
    import json

    return json.loads(line)


if __name__ == "__main__":
    unittest.main()
