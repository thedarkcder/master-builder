from __future__ import annotations

import base64
import binascii
import json
import mimetypes
from dataclasses import dataclass
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.parse import unquote, urljoin, urlparse
from urllib.request import Request, urlopen

from orchestrator.core.config import Settings


class VoiceReplyError(RuntimeError):
    pass


@dataclass(frozen=True)
class VoiceReplyAudio:
    audio_bytes: bytes
    filename: str
    content_type: str


def synthesize_reply_audio(*, settings: Settings, text: str) -> VoiceReplyAudio:
    provider = str(settings.voice_reply_provider or "").strip().lower()
    if provider in {"", "disabled"}:
        raise VoiceReplyError("Voice reply is disabled")
    if provider == "pocket_tts":
        return _synthesize_with_pocket_tts(settings=settings, text=text)
    raise VoiceReplyError(f"Unsupported voice reply provider '{provider}'")


def _synthesize_with_pocket_tts(*, settings: Settings, text: str) -> VoiceReplyAudio:
    base_url = str(settings.pocket_tts_base_url or "").strip()
    if not base_url:
        raise VoiceReplyError("Pocket TTS base URL is missing")
    normalized_text = text.strip()
    if not normalized_text:
        raise VoiceReplyError("Voice reply text cannot be empty")

    payload: dict[str, str] = {"text": normalized_text}
    voice = str(settings.pocket_tts_voice or "").strip()
    if voice:
        payload["voice"] = voice

    request = Request(
        url=base_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Accept": "audio/*,application/json",
            "Content-Type": "application/json",
            "User-Agent": "MasterBuilderVoice/1.0",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=45) as response:
            raw_body = response.read()
            response_content_type = _normalize_content_type(response.headers.get("Content-Type"))
            response_content_disposition = str(response.headers.get("Content-Disposition") or "")
    except HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="ignore")
        raise VoiceReplyError(f"Pocket TTS request failed ({exc.code}): {error_body}") from exc
    except URLError as exc:
        raise VoiceReplyError(f"Pocket TTS request failed (network): {exc}") from exc

    if response_content_type.startswith("audio/") and raw_body:
        filename = _pick_filename(
            _filename_from_content_disposition(response_content_disposition),
            _filename_from_content_type(response_content_type),
        )
        return VoiceReplyAudio(
            audio_bytes=raw_body,
            filename=filename,
            content_type=response_content_type,
        )

    payload_data = _parse_json_payload(raw_body=raw_body, response_content_type=response_content_type)
    if payload_data is not None:
        if not isinstance(payload_data, dict):
            raise VoiceReplyError("Pocket TTS JSON response was not an object")
        return _audio_from_json(
            payload=payload_data,
            base_url=base_url,
            response_content_type=response_content_type,
            response_content_disposition=response_content_disposition,
        )

    if not raw_body:
        raise VoiceReplyError("Pocket TTS response did not include audio payload")
    fallback_content_type = response_content_type or "audio/mpeg"
    fallback_filename = _pick_filename(
        _filename_from_content_disposition(response_content_disposition),
        _filename_from_content_type(fallback_content_type),
    )
    return VoiceReplyAudio(
        audio_bytes=raw_body,
        filename=fallback_filename,
        content_type=fallback_content_type,
    )


def _audio_from_json(
    *,
    payload: dict,
    base_url: str,
    response_content_type: str,
    response_content_disposition: str,
) -> VoiceReplyAudio:
    content_type_hint = _normalize_content_type(payload.get("content_type"))
    filename_hint = _sanitize_filename(str(payload.get("filename") or ""))

    encoded_audio = str(payload.get("audio_base64") or "").strip()
    if encoded_audio:
        audio_bytes, data_url_content_type = _decode_audio_base64(encoded_audio)
        content_type = content_type_hint or data_url_content_type or response_content_type or "audio/mpeg"
        filename = _pick_filename(
            filename_hint,
            _filename_from_content_disposition(response_content_disposition),
            _filename_from_content_type(content_type),
        )
        return VoiceReplyAudio(audio_bytes=audio_bytes, filename=filename, content_type=content_type)

    audio_url = str(payload.get("audio_url") or "").strip()
    if audio_url:
        return _fetch_audio_from_url(
            base_url=base_url,
            audio_url=audio_url,
            filename_hint=filename_hint,
            content_type_hint=content_type_hint,
        )

    raise VoiceReplyError("Pocket TTS JSON response missing audio_base64/audio_url")


def _fetch_audio_from_url(
    *,
    base_url: str,
    audio_url: str,
    filename_hint: str | None,
    content_type_hint: str,
) -> VoiceReplyAudio:
    resolved_url = urljoin(base_url, audio_url)
    request = Request(
        url=resolved_url,
        headers={
            "Accept": "audio/*,application/octet-stream",
            "User-Agent": "MasterBuilderVoice/1.0",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=45) as response:
            audio_bytes = response.read()
            response_content_type = _normalize_content_type(response.headers.get("Content-Type"))
            response_disposition = str(response.headers.get("Content-Disposition") or "")
    except HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="ignore")
        raise VoiceReplyError(f"Pocket TTS audio_url fetch failed ({exc.code}): {error_body}") from exc
    except URLError as exc:
        raise VoiceReplyError(f"Pocket TTS audio_url fetch failed (network): {exc}") from exc

    if not audio_bytes:
        raise VoiceReplyError("Pocket TTS audio_url returned an empty payload")
    content_type = _normalize_content_type(content_type_hint) or response_content_type or "audio/mpeg"
    filename = _pick_filename(
        filename_hint,
        _filename_from_content_disposition(response_disposition),
        _filename_from_url(resolved_url),
        _filename_from_content_type(content_type),
    )
    return VoiceReplyAudio(audio_bytes=audio_bytes, filename=filename, content_type=content_type)


def _parse_json_payload(*, raw_body: bytes, response_content_type: str) -> dict | list | None:
    if not raw_body:
        return None
    try:
        decoded = raw_body.decode("utf-8")
    except UnicodeDecodeError:
        if response_content_type == "application/json":
            raise VoiceReplyError("Pocket TTS response returned invalid JSON")
        return None
    try:
        return json.loads(decoded)
    except json.JSONDecodeError:
        if response_content_type == "application/json":
            raise VoiceReplyError("Pocket TTS response returned invalid JSON")
        return None


def _decode_audio_base64(value: str) -> tuple[bytes, str]:
    content_type = ""
    payload = value.strip()
    if payload.startswith("data:") and ";base64," in payload:
        header, payload = payload.split(",", 1)
        content_type = _normalize_content_type(header[5:].split(";", 1)[0])
    try:
        decoded = base64.b64decode(payload, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise VoiceReplyError("Pocket TTS audio_base64 payload was invalid") from exc
    if not decoded:
        raise VoiceReplyError("Pocket TTS audio_base64 payload was empty")
    return decoded, content_type


def _normalize_content_type(value: object) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    return raw.split(";", 1)[0].strip()


def _filename_from_content_disposition(content_disposition: str) -> str | None:
    header = content_disposition.strip()
    if not header:
        return None
    for part in header.split(";"):
        token = part.strip()
        if token.lower().startswith("filename*="):
            raw_value = token.split("=", 1)[1].strip().strip('"').strip("'")
            _, _, encoded_name = raw_value.partition("''")
            candidate = unquote(encoded_name or raw_value)
            sanitized = _sanitize_filename(candidate)
            if sanitized:
                return sanitized
        if token.lower().startswith("filename="):
            candidate = token.split("=", 1)[1].strip().strip('"').strip("'")
            sanitized = _sanitize_filename(candidate)
            if sanitized:
                return sanitized
    return None


def _filename_from_url(url: str) -> str | None:
    parsed_path = urlparse(url).path
    if not parsed_path:
        return None
    return _sanitize_filename(parsed_path.rsplit("/", 1)[-1])


def _filename_from_content_type(content_type: str) -> str:
    extension = mimetypes.guess_extension(_normalize_content_type(content_type)) or ".bin"
    return f"voice_reply{extension}"


def _sanitize_filename(value: str) -> str | None:
    candidate = value.strip().strip('"').strip("'")
    if not candidate:
        return None
    candidate = candidate.replace("\\", "/").rsplit("/", 1)[-1].strip()
    return candidate or None


def _pick_filename(*candidates: str | None) -> str:
    for candidate in candidates:
        sanitized = _sanitize_filename(candidate or "")
        if sanitized:
            return sanitized
    return "voice_reply.bin"
