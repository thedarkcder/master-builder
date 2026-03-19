from __future__ import annotations

import io
import math
import struct
import unittest
import wave
from types import SimpleNamespace
from unittest.mock import patch

from orchestrator.core.discord.live_voice_service import (
    ConfiguredLiveVoiceRoom,
    DiscordLiveVoiceService,
    _wav_bytes_to_discord_pcm,
)


def _build_wav_bytes(*, sample_rate_hz: int, channels: int, duration_seconds: float) -> bytes:
    frame_count = int(sample_rate_hz * duration_seconds)
    samples: list[int] = []
    for index in range(frame_count):
        value = int(12_000 * math.sin(2 * math.pi * 220 * index / sample_rate_hz))
        if channels == 1:
            samples.append(value)
        else:
            samples.extend([value, value])

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate_hz)
        wav_file.writeframes(struct.pack("<" + "h" * len(samples), *samples))
    return buffer.getvalue()


class LiveVoiceServiceTests(unittest.TestCase):
    def test_wav_bytes_to_discord_pcm_resamples_mono_wav_to_stereo_48khz(self) -> None:
        wav_bytes = _build_wav_bytes(sample_rate_hz=24_000, channels=1, duration_seconds=0.1)

        pcm_bytes = _wav_bytes_to_discord_pcm(wav_bytes)

        expected_frame_count = int(48_000 * 0.1)
        self.assertEqual(len(pcm_bytes), expected_frame_count * 2 * 2)
        self.assertNotEqual(pcm_bytes, b"\x00" * len(pcm_bytes))


class _FakeClientException(Exception):
    pass


class _FakeVoiceClient:
    def __init__(self, *, channel_id: str, connected: bool = True) -> None:
        self.channel = SimpleNamespace(id=channel_id)
        self._connected = connected
        self.start_recording_calls = 0
        self.stop_recording_calls = 0
        self.disconnect_calls = 0

    def is_connected(self) -> bool:
        return self._connected

    def start_recording(self, *_args, **_kwargs) -> None:
        self.start_recording_calls += 1

    def stop_recording(self) -> None:
        self.stop_recording_calls += 1

    async def disconnect(self, *, force: bool = True) -> None:
        self.disconnect_calls += 1
        self._connected = False


class _FakeGuild:
    def __init__(self, *, voice_client=None) -> None:
        self.voice_client = voice_client


class _FakeVoiceChannel:
    def __init__(self, *, channel_id: str, guild: _FakeGuild, connect_side_effects: list[object]) -> None:
        self.id = channel_id
        self.guild = guild
        self.members: list[object] = []
        self._connect_side_effects = list(connect_side_effects)
        self.connect_calls = 0

    async def connect(self):
        self.connect_calls += 1
        if not self._connect_side_effects:
            raise AssertionError("No connect side effect configured")
        result = self._connect_side_effects.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class LiveVoiceJoinTests(unittest.IsolatedAsyncioTestCase):
    def _build_service(self) -> DiscordLiveVoiceService:
        service = DiscordLiveVoiceService(
            settings=SimpleNamespace(),
            session_factory=lambda: None,
        )
        service._discord_module = SimpleNamespace(ClientException=_FakeClientException)
        service._discord_client = SimpleNamespace(user=SimpleNamespace(id="bot-user"))
        return service

    async def test_join_room_reuses_existing_connected_guild_voice_client(self) -> None:
        service = self._build_service()
        room = ConfiguredLiveVoiceRoom(
            tenant_id="tenant-a",
            guild_id="guild-1",
            voice_channel_id="voice-1",
            linked_text_channel_id="text-1",
            project_id="project-1",
        )
        service._runtime.register_room(binding=room.binding, bot_user_id="bot-user", human_member_count=1)
        voice_client = _FakeVoiceClient(channel_id=room.voice_channel_id, connected=True)
        guild = _FakeGuild(voice_client=voice_client)
        voice_channel = _FakeVoiceChannel(
            channel_id=room.voice_channel_id,
            guild=guild,
            connect_side_effects=[_FakeClientException("Already connected to a voice channel.")],
        )
        service._resolve_voice_channel = lambda _: voice_channel

        with patch("orchestrator.core.discord.live_voice_service._build_sink", return_value=object()):
            await service._join_room(room=room, human_count=2)

        self.assertIs(service._voice_clients_by_room_key[room.room_key], voice_client)
        self.assertEqual(service._voice_clients_by_guild_id[room.guild_id], room.room_key)
        self.assertIn(room.room_key, service._recording_room_keys)
        self.assertEqual(voice_client.start_recording_calls, 1)
        self.assertEqual(voice_channel.connect_calls, 0)

    async def test_join_room_disconnects_stale_guild_voice_client_and_reconnects(self) -> None:
        service = self._build_service()
        room = ConfiguredLiveVoiceRoom(
            tenant_id="tenant-a",
            guild_id="guild-1",
            voice_channel_id="voice-1",
            linked_text_channel_id="text-1",
            project_id="project-1",
        )
        service._runtime.register_room(binding=room.binding, bot_user_id="bot-user", human_member_count=1)
        stale_voice_client = _FakeVoiceClient(channel_id=room.voice_channel_id, connected=False)
        replacement_voice_client = _FakeVoiceClient(channel_id=room.voice_channel_id, connected=True)
        guild = _FakeGuild(voice_client=stale_voice_client)
        voice_channel = _FakeVoiceChannel(
            channel_id=room.voice_channel_id,
            guild=guild,
            connect_side_effects=[replacement_voice_client],
        )
        service._resolve_voice_channel = lambda _: voice_channel

        with patch("orchestrator.core.discord.live_voice_service._build_sink", return_value=object()):
            await service._join_room(room=room, human_count=1)

        self.assertEqual(stale_voice_client.disconnect_calls, 1)
        self.assertIs(service._voice_clients_by_room_key[room.room_key], replacement_voice_client)
        self.assertEqual(replacement_voice_client.start_recording_calls, 1)
        self.assertEqual(voice_channel.connect_calls, 1)


if __name__ == "__main__":
    unittest.main()
