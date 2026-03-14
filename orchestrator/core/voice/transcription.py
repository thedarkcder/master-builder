from __future__ import annotations

import json
import mimetypes
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.request import Request, urlopen

from orchestrator.core.config import Settings


class VoiceTranscriptionError(RuntimeError):
    pass


def download_audio_bytes(*, url: str, bot_token: str | None = None) -> tuple[bytes, str]:
    normalized_url = str(url or "").strip()
    if not normalized_url:
        raise VoiceTranscriptionError("Audio attachment URL is missing")
    headers = {
        "Accept": "*/*",
        "User-Agent": "MasterBuilderVoice/1.0",
    }
    if bot_token and bot_token.strip():
        headers["Authorization"] = f"Bot {bot_token.strip()}"
    request = Request(
        url=normalized_url,
        headers=headers,
        method="GET",
    )
    try:
        with urlopen(request, timeout=45) as response:
            payload = response.read()
            content_type = str(response.headers.get("Content-Type") or "").strip()
    except HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="ignore")
        raise VoiceTranscriptionError(f"Audio download failed ({exc.code}): {error_body}") from exc
    except URLError as exc:
        raise VoiceTranscriptionError(f"Audio download failed (network): {exc}") from exc
    if not payload:
        raise VoiceTranscriptionError("Downloaded audio payload was empty")
    return payload, content_type


def transcribe_audio_bytes(
    *,
    settings: Settings,
    audio_bytes: bytes,
    filename: str,
    content_type: str | None = None,
) -> str:
    provider = str(settings.voice_transcription_provider or "").strip().lower()
    if provider in {"", "disabled"}:
        raise VoiceTranscriptionError("Voice transcription is disabled")
    if provider == "openai":
        return _transcribe_with_openai(
            settings=settings,
            audio_bytes=audio_bytes,
            filename=filename,
            content_type=content_type,
        )
    raise VoiceTranscriptionError(f"Unsupported transcription provider '{provider}'")


def _transcribe_with_openai(
    *,
    settings: Settings,
    audio_bytes: bytes,
    filename: str,
    content_type: str | None,
) -> str:
    api_key = str(settings.voice_transcription_openai_api_key or "").strip()
    if not api_key:
        raise VoiceTranscriptionError("OpenAI transcription API key is missing")
    if not audio_bytes:
        raise VoiceTranscriptionError("Audio payload is empty")
    if len(audio_bytes) > int(settings.voice_attachment_max_bytes):
        raise VoiceTranscriptionError("Audio payload exceeds configured maximum size")

    model = str(settings.voice_transcription_model or "").strip() or "gpt-4o-mini-transcribe"
    language = str(settings.voice_transcription_language or "").strip()
    inferred_content_type = content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    body, boundary = _encode_multipart_form_data(
        fields={
            "model": model,
            "response_format": "json",
            **({"language": language} if language else {}),
        },
        file_field="file",
        file_name=filename,
        file_content_type=inferred_content_type,
        file_bytes=audio_bytes,
    )

    request = Request(
        url="https://api.openai.com/v1/audio/transcriptions",
        data=body,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": "MasterBuilderVoice/1.0",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=45) as response:
            raw_body = response.read().decode("utf-8")
    except HTTPError as exc:
        error_body = exc.read().decode("utf-8")
        raise VoiceTranscriptionError(f"OpenAI transcription failed ({exc.code}): {error_body}") from exc
    except URLError as exc:
        raise VoiceTranscriptionError(f"OpenAI transcription failed (network): {exc}") from exc

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise VoiceTranscriptionError("OpenAI transcription returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise VoiceTranscriptionError("OpenAI transcription response was not an object")
    text = str(payload.get("text") or "").strip()
    if not text:
        raise VoiceTranscriptionError("OpenAI transcription response did not include text")
    return text


def _encode_multipart_form_data(
    *,
    fields: dict[str, str],
    file_field: str,
    file_name: str,
    file_content_type: str,
    file_bytes: bytes,
) -> tuple[bytes, str]:
    boundary = "----master-builder-voice-boundary"
    parts: list[bytes] = []
    for key, value in fields.items():
        parts.extend(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8"),
                f"{value}\r\n".encode("utf-8"),
            ]
        )
    parts.extend(
        [
            f"--{boundary}\r\n".encode("utf-8"),
            (
                f'Content-Disposition: form-data; name="{file_field}"; filename="{file_name}"\r\n'
                f"Content-Type: {file_content_type}\r\n\r\n"
            ).encode("utf-8"),
            file_bytes,
            b"\r\n",
            f"--{boundary}--\r\n".encode("utf-8"),
        ]
    )
    return b"".join(parts), boundary
