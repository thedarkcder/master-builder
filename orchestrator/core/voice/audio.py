from __future__ import annotations

from pathlib import Path

_SUPPORTED_AUDIO_EXTENSIONS = {
    ".aac",
    ".flac",
    ".m4a",
    ".mp3",
    ".mp4",
    ".oga",
    ".ogg",
    ".opus",
    ".wav",
    ".webm",
}


def is_supported_audio_attachment(attachment: dict[str, str]) -> bool:
    content_type = str(attachment.get("content_type") or "").strip().lower()
    if content_type.startswith("audio/"):
        return True
    filename = str(attachment.get("filename") or "").strip().lower()
    if not filename:
        return False
    return Path(filename).suffix in _SUPPORTED_AUDIO_EXTENSIONS


def select_first_supported_audio_attachment(attachments: list[dict[str, str]]) -> dict[str, str] | None:
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        if is_supported_audio_attachment(attachment):
            return attachment
    return None

