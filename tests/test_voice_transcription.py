from __future__ import annotations

from io import BytesIO
import unittest
from urllib.error import HTTPError
from unittest.mock import patch

from orchestrator.core.config import Settings
from orchestrator.core.voice.transcription import (
    VoiceTranscriptionError,
    _load_whisper_model,
    ensure_transcription_provider_ready,
    transcribe_audio_bytes,
)


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
        settings = Settings(voice_stt_provider="disabled")
        with self.assertRaisesRegex(VoiceTranscriptionError, "disabled"):
            transcribe_audio_bytes(
                settings=settings,
                audio_bytes=b"a",
                filename="note.ogg",
            )

    def test_transcribe_openai_requires_api_key(self) -> None:
        settings = Settings(
            voice_stt_provider="openai",
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
            voice_stt_provider="openai",
            voice_transcription_openai_api_key="test-key",
            voice_transcription_model="gpt-4o-mini-transcribe",
        )
        with patch(
            "orchestrator.core.voice.transcription.urlopen", side_effect=_fake_urlopen
        ):
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
            voice_stt_provider="openai",
            voice_transcription_openai_api_key="test-key",
        )
        with patch(
            "orchestrator.core.voice.transcription.urlopen",
            return_value=_FakeResponse(b"not-json"),
        ):
            with self.assertRaisesRegex(VoiceTranscriptionError, "invalid JSON"):
                transcribe_audio_bytes(
                    settings=settings,
                    audio_bytes=b"audio",
                    filename="voice.ogg",
                )

    def test_transcribe_openai_http_error_surfaces(self) -> None:
        settings = Settings(
            voice_stt_provider="openai",
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

    def test_transcribe_whisper_uses_local_model_and_joins_segments(self) -> None:
        class _Segment:
            def __init__(self, text: str) -> None:
                self.text = text

        class _Model:
            def transcribe(self, audio_array, language=None):  # noqa: ANN001
                self.audio_array = audio_array
                self.language = language
                return iter([_Segment("Build"), _Segment("the PM room")]), object()

        settings = Settings(
            voice_stt_provider="whisper",
            voice_transcription_model="base",
            voice_transcription_language="en",
        )
        model = _Model()
        with (
            patch(
                "orchestrator.core.voice.transcription._get_whisper_model",
                return_value=model,
            ),
            patch(
                "orchestrator.core.voice.transcription._decode_audio_to_float32_mono",
                return_value=[0.0, 1.0],
            ),
        ):
            text = transcribe_audio_bytes(
                settings=settings,
                audio_bytes=b"audio-bytes",
                filename="voice.wav",
                content_type="audio/wav",
            )
        self.assertEqual(text, "Build the PM room")
        self.assertEqual(model.language, "en")

    def test_ensure_transcription_provider_ready_loads_whisper_model(self) -> None:
        settings = Settings(
            voice_stt_provider="whisper",
            voice_transcription_model="small",
        )
        with patch(
            "orchestrator.core.voice.transcription._get_whisper_model",
            return_value=object(),
        ) as model_mock:
            ensure_transcription_provider_ready(settings=settings)
        model_mock.assert_called_once_with(settings=settings, allow_download=False)

    def test_ensure_transcription_provider_ready_surfaces_whisper_model_failure(
        self,
    ) -> None:
        settings = Settings(
            voice_stt_provider="whisper",
            voice_transcription_model="base",
        )
        with patch(
            "orchestrator.core.voice.transcription._get_whisper_model",
            side_effect=VoiceTranscriptionError("model load failed"),
        ):
            with self.assertRaisesRegex(VoiceTranscriptionError, "model load failed"):
                ensure_transcription_provider_ready(settings=settings)

    def test_load_whisper_model_stays_cache_only_at_runtime(self) -> None:
        calls: list[dict[str, object]] = []

        class _WhisperModel:
            def __init__(self, model_name, **kwargs):  # noqa: ANN001
                calls.append({"model_name": model_name, **kwargs})
                raise RuntimeError("missing local cache")

        faster_whisper = type("FW", (), {"WhisperModel": _WhisperModel})()

        with self.assertRaisesRegex(RuntimeError, "missing local cache"):
            _load_whisper_model(
                faster_whisper=faster_whisper,
                model_name="base",
                device="cpu",
                compute_type="int8",
                allow_download=False,
            )

        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]["local_files_only"])

    def test_load_whisper_model_prewarm_tries_local_cache_before_download(self) -> None:
        calls: list[dict[str, object]] = []

        class _WhisperModel:
            def __init__(self, model_name, **kwargs):  # noqa: ANN001
                calls.append({"model_name": model_name, **kwargs})
                if kwargs.get("local_files_only"):
                    raise RuntimeError("missing local cache")
                self.model_name = model_name

        faster_whisper = type("FW", (), {"WhisperModel": _WhisperModel})()

        model = _load_whisper_model(
            faster_whisper=faster_whisper,
            model_name="base",
            device="cpu",
            compute_type="int8",
            allow_download=True,
        )

        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0]["local_files_only"])
        self.assertFalse(calls[1]["local_files_only"])
        self.assertEqual(model.model_name, "base")


if __name__ == "__main__":
    unittest.main()
