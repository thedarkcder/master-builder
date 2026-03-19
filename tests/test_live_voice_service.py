from __future__ import annotations

import io
import math
import struct
import unittest
import wave

from orchestrator.core.discord.live_voice_service import _wav_bytes_to_discord_pcm


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


if __name__ == "__main__":
    unittest.main()
