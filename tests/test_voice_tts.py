from __future__ import annotations

import base64
import json
import unittest
from io import BytesIO
from urllib.error import HTTPError
from urllib.error import URLError
from unittest.mock import patch

from orchestrator.core.config import Settings
from orchestrator.core.voice.tts import VoiceReplyError, synthesize_reply_audio


class _FakeResponse:
    def __init__(self, body: bytes, headers: dict[str, str] | None = None) -> None:
        self._body = body
        self.headers = headers or {}

    def read(self) -> bytes:
        return self._body

    def __enter__(self):  # noqa: ANN204
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001, ANN204
        return False


class VoiceTtsTests(unittest.TestCase):
    def test_synthesize_reply_rejects_disabled_provider(self) -> None:
        settings = Settings(voice_reply_provider="disabled")
        with self.assertRaisesRegex(VoiceReplyError, "disabled"):
            synthesize_reply_audio(settings=settings, text="hello")

    def test_synthesize_reply_pocket_tts_requires_base_url(self) -> None:
        settings = Settings(voice_reply_provider="pocket_tts", pocket_tts_base_url="")
        with self.assertRaisesRegex(VoiceReplyError, "base URL"):
            synthesize_reply_audio(settings=settings, text="hello")

    def test_synthesize_reply_accepts_raw_audio_response(self) -> None:
        captured: dict[str, object] = {}

        def _fake_urlopen(request, timeout=45):  # noqa: ANN001, ARG001
            captured["method"] = request.get_method()
            captured["url"] = request.full_url
            captured["headers"] = dict(request.header_items())
            captured["body"] = request.data
            return _FakeResponse(
                b"ID3-audio-data",
                headers={
                    "Content-Type": "audio/mpeg",
                    "Content-Disposition": 'attachment; filename="reply.mp3"',
                },
            )

        settings = Settings(
            voice_reply_provider="pocket_tts",
            pocket_tts_base_url="https://tts.example/synthesize",
            pocket_tts_voice="alloy",
        )
        with patch("orchestrator.core.voice.tts.urlopen", side_effect=_fake_urlopen):
            audio = synthesize_reply_audio(settings=settings, text=" hello ")

        self.assertEqual(audio.audio_bytes, b"ID3-audio-data")
        self.assertEqual(audio.filename, "reply.mp3")
        self.assertEqual(audio.content_type, "audio/mpeg")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(str(captured["url"]), "https://tts.example/synthesize")
        self.assertEqual(
            json.loads(bytes(captured["body"] or b"").decode("utf-8")),
            {"text": "hello", "voice": "alloy"},
        )
        self.assertIn("application/json", str(captured["headers"]))

    def test_synthesize_reply_accepts_json_audio_base64(self) -> None:
        encoded = base64.b64encode(b"WAVE").decode("ascii")
        response_body = json.dumps(
            {
                "audio_base64": f"data:audio/wav;base64,{encoded}",
                "filename": "reply.wav",
            }
        ).encode("utf-8")
        settings = Settings(
            voice_reply_provider="pocket_tts",
            pocket_tts_base_url="https://tts.example/synthesize",
        )
        with patch(
            "orchestrator.core.voice.tts.urlopen",
            return_value=_FakeResponse(response_body, headers={"Content-Type": "application/json"}),
        ):
            audio = synthesize_reply_audio(settings=settings, text="hello")
        self.assertEqual(audio.audio_bytes, b"WAVE")
        self.assertEqual(audio.filename, "reply.wav")
        self.assertEqual(audio.content_type, "audio/wav")

    def test_synthesize_reply_accepts_json_audio_url(self) -> None:
        calls: list[tuple[str, str]] = []

        def _fake_urlopen(request, timeout=45):  # noqa: ANN001, ARG001
            calls.append((request.get_method(), str(request.full_url)))
            if request.get_method() == "POST":
                return _FakeResponse(
                    b'{"audio_url":"/files/reply.ogg"}',
                    headers={"Content-Type": "application/json"},
                )
            return _FakeResponse(
                b"OggS-audio-data",
                headers={"Content-Type": "audio/ogg"},
            )

        settings = Settings(
            voice_reply_provider="pocket_tts",
            pocket_tts_base_url="https://tts.example/synthesize",
        )
        with patch("orchestrator.core.voice.tts.urlopen", side_effect=_fake_urlopen):
            audio = synthesize_reply_audio(settings=settings, text="hello")

        self.assertEqual(audio.audio_bytes, b"OggS-audio-data")
        self.assertEqual(audio.filename, "reply.ogg")
        self.assertEqual(audio.content_type, "audio/ogg")
        self.assertEqual(
            calls,
            [
                ("POST", "https://tts.example/synthesize"),
                ("GET", "https://tts.example/files/reply.ogg"),
            ],
        )

    def test_synthesize_reply_invalid_json_and_payload_errors(self) -> None:
        settings = Settings(
            voice_reply_provider="pocket_tts",
            pocket_tts_base_url="https://tts.example/synthesize",
        )
        with patch(
            "orchestrator.core.voice.tts.urlopen",
            return_value=_FakeResponse(b"not-json", headers={"Content-Type": "application/json"}),
        ):
            with self.assertRaisesRegex(VoiceReplyError, "invalid JSON"):
                synthesize_reply_audio(settings=settings, text="hello")

        with patch(
            "orchestrator.core.voice.tts.urlopen",
            return_value=_FakeResponse(b"{}", headers={"Content-Type": "application/json"}),
        ):
            with self.assertRaisesRegex(VoiceReplyError, "missing audio_base64/audio_url"):
                synthesize_reply_audio(settings=settings, text="hello")

        with patch(
            "orchestrator.core.voice.tts.urlopen",
            return_value=_FakeResponse(
                b'{"audio_base64":"!!!"}',
                headers={"Content-Type": "application/json"},
            ),
        ):
            with self.assertRaisesRegex(VoiceReplyError, "invalid"):
                synthesize_reply_audio(settings=settings, text="hello")

    def test_synthesize_reply_surfaces_http_and_network_errors(self) -> None:
        settings = Settings(
            voice_reply_provider="pocket_tts",
            pocket_tts_base_url="https://tts.example/synthesize",
        )
        http_error = HTTPError(
            url="https://tts.example/synthesize",
            code=500,
            msg="Server Error",
            hdrs=None,
            fp=BytesIO(b"downstream failure"),
        )
        with patch("orchestrator.core.voice.tts.urlopen", side_effect=http_error):
            with self.assertRaisesRegex(VoiceReplyError, "500"):
                synthesize_reply_audio(settings=settings, text="hello")

        with patch("orchestrator.core.voice.tts.urlopen", side_effect=URLError("network down")):
            with self.assertRaisesRegex(VoiceReplyError, "network"):
                synthesize_reply_audio(settings=settings, text="hello")


if __name__ == "__main__":
    unittest.main()
