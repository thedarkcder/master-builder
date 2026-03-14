from __future__ import annotations

from io import BytesIO
import unittest
from urllib.error import HTTPError
from unittest.mock import patch

from orchestrator.core.config import Settings
from orchestrator.core.voice.transcription import VoiceTranscriptionError, transcribe_audio_bytes


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):  # noqa: ANN204
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001, ANN204
        return False


class VoiceTranscriptionTests(unittest.TestCase):
    def test_transcribe_rejects_disabled_provider(self) -> None:
        settings = Settings(voice_transcription_provider="disabled")
        with self.assertRaisesRegex(VoiceTranscriptionError, "disabled"):
            transcribe_audio_bytes(
                settings=settings,
                audio_bytes=b"a",
                filename="note.ogg",
            )

    def test_transcribe_openai_requires_api_key(self) -> None:
        settings = Settings(
            voice_transcription_provider="openai",
            voice_transcription_openai_api_key="",
        )
        with self.assertRaisesRegex(VoiceTranscriptionError, "API key"):
            transcribe_audio_bytes(
                settings=settings,
                audio_bytes=b"a",
                filename="note.ogg",
            )

    def test_transcribe_openai_parses_text_response(self) -> None:
        captured: dict[str, object] = {}

        def _fake_urlopen(request, timeout=45):  # noqa: ANN001, ARG001
            captured["method"] = request.get_method()
            captured["url"] = request.full_url
            captured["headers"] = dict(request.header_items())
            captured["body"] = request.data
            return _FakeResponse(b'{"text":"Build a lighter onboarding flow"}')

        settings = Settings(
            voice_transcription_provider="openai",
            voice_transcription_openai_api_key="test-key",
            voice_transcription_model="gpt-4o-mini-transcribe",
        )
        with patch("orchestrator.core.voice.transcription.urlopen", side_effect=_fake_urlopen):
            text = transcribe_audio_bytes(
                settings=settings,
                audio_bytes=b"audio-bytes",
                filename="voice.ogg",
                content_type="audio/ogg",
            )
        self.assertEqual(text, "Build a lighter onboarding flow")
        self.assertEqual(captured["method"], "POST")
        self.assertIn("/v1/audio/transcriptions", str(captured["url"]))
        self.assertIn("multipart/form-data", str(captured["headers"]).lower())
        self.assertIn(b'filename="voice.ogg"', bytes(captured["body"] or b""))

    def test_transcribe_openai_invalid_json_is_error(self) -> None:
        settings = Settings(
            voice_transcription_provider="openai",
            voice_transcription_openai_api_key="test-key",
        )
        with patch("orchestrator.core.voice.transcription.urlopen", return_value=_FakeResponse(b"not-json")):
            with self.assertRaisesRegex(VoiceTranscriptionError, "invalid JSON"):
                transcribe_audio_bytes(
                    settings=settings,
                    audio_bytes=b"audio",
                    filename="voice.ogg",
                )

    def test_transcribe_openai_http_error_surfaces(self) -> None:
        settings = Settings(
            voice_transcription_provider="openai",
            voice_transcription_openai_api_key="test-key",
        )
        error = HTTPError(
            url="https://api.openai.com/v1/audio/transcriptions",
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=BytesIO(b"bad key"),
        )
        with patch("orchestrator.core.voice.transcription.urlopen", side_effect=error):
            with self.assertRaisesRegex(VoiceTranscriptionError, "401"):
                transcribe_audio_bytes(
                    settings=settings,
                    audio_bytes=b"audio",
                    filename="voice.ogg",
                )


if __name__ == "__main__":
    unittest.main()

