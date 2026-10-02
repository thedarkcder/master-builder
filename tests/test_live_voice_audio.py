from __future__ import annotations

import io
import struct
import unittest
import wave

from orchestrator.core.discord.live_voice_audio import wav_duration_seconds


def _build_wav_bytes(
    *, sample_rate_hz: int, channels: int, duration_seconds: float
) -> bytes:
    frame_count = int(sample_rate_hz * duration_seconds)
    samples = [0] * frame_count * channels
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate_hz)
        wav_file.writeframes(struct.pack("<" + "h" * len(samples), *samples))
    return buffer.getvalue()


class LiveVoiceAudioTests(unittest.TestCase):
    def test_wav_duration_seconds_reads_header_duration(self) -> None:
        wav_bytes = _build_wav_bytes(
            sample_rate_hz=48_000, channels=2, duration_seconds=0.25
        )

        self.assertAlmostEqual(wav_duration_seconds(wav_bytes), 0.25, places=3)


if __name__ == "__main__":
    unittest.main()
