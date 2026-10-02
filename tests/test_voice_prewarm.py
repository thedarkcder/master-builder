from __future__ import annotations

import unittest
from unittest.mock import patch

from orchestrator.core.config import Settings
from orchestrator.core.voice.prewarm import prewarm_voice_dependencies


class VoicePrewarmTests(unittest.TestCase):
    def test_prewarm_loads_enabled_transcription_and_voice_dependencies(self) -> None:
        settings = Settings(
            voice_stt_provider="disabled",
            voice_tts_provider="pocket_tts",
        )

        with (
            patch(
                "orchestrator.core.voice.prewarm.ensure_transcription_provider_ready"
            ) as transcription_mock,
            patch(
                "orchestrator.core.voice.prewarm.ensure_voice_reply_provider_ready",
                return_value=["alba", "jean"],
            ) as tts_mock,
        ):
            result = prewarm_voice_dependencies(settings=settings)

        transcription_mock.assert_not_called()
        tts_mock.assert_called_once_with(settings=settings, allow_download=True)
        self.assertEqual(result.voice_stt_provider, "disabled")
        self.assertEqual(result.voice_tts_provider, "pocket_tts")
        self.assertFalse(result.transcription_ready)
        self.assertEqual(result.prewarmed_voice_ids, ("alba", "jean"))

    def test_prewarm_skips_disabled_dependencies(self) -> None:
        settings = Settings(
            voice_stt_provider="disabled",
            voice_tts_provider="disabled",
        )

        with (
            patch(
                "orchestrator.core.voice.prewarm.ensure_transcription_provider_ready"
            ) as transcription_mock,
            patch(
                "orchestrator.core.voice.prewarm.ensure_voice_reply_provider_ready"
            ) as tts_mock,
        ):
            result = prewarm_voice_dependencies(settings=settings)

        transcription_mock.assert_not_called()
        tts_mock.assert_not_called()
        self.assertEqual(result.voice_stt_provider, "disabled")
        self.assertEqual(result.voice_tts_provider, "disabled")
        self.assertFalse(result.transcription_ready)
        self.assertEqual(result.prewarmed_voice_ids, ())


if __name__ == "__main__":
    unittest.main()
