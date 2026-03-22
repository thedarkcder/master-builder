from __future__ import annotations

from orchestrator.core.voice.audio import is_supported_audio_attachment, select_first_supported_audio_attachment


def test_is_supported_audio_attachment_by_content_type() -> None:
    assert is_supported_audio_attachment({"content_type": "audio/ogg", "filename": "note.bin"}) is True


def test_is_supported_audio_attachment_by_extension() -> None:
    assert is_supported_audio_attachment({"content_type": "application/octet-stream", "filename": "note.m4a"}) is True
    assert is_supported_audio_attachment({"content_type": "text/plain", "filename": "note.txt"}) is False


def test_select_first_supported_audio_attachment() -> None:
    selected = select_first_supported_audio_attachment(
        [
            {"filename": "trace.txt", "content_type": "text/plain"},
            {"filename": "voice.ogg", "content_type": "audio/ogg", "url": "https://cdn/voice.ogg"},
        ]
    )
    assert selected is not None
    assert selected["filename"] == "voice.ogg"

