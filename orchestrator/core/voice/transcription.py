from __future__ import annotations

import io
import json
import mimetypes
import threading
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.request import Request, urlopen

from orchestrator.core.config import Settings


class VoiceTranscriptionError(RuntimeError):
    pass


_WHISPER_MODEL_LOCK = threading.RLock()
_WHISPER_MODELS: dict[tuple[str, str, str], object] = {}


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
    if provider == "whisper":
        return _transcribe_with_whisper(
            settings=settings,
            audio_bytes=audio_bytes,
            filename=filename,
            content_type=content_type,
        )
    raise VoiceTranscriptionError(f"Unsupported transcription provider '{provider}'")


def ensure_transcription_provider_ready(*, settings: Settings) -> None:
    provider = str(settings.voice_transcription_provider or "").strip().lower()
    if provider in {"", "disabled"}:
        raise VoiceTranscriptionError("Voice transcription is disabled")
    if provider == "openai":
        api_key = str(settings.voice_transcription_openai_api_key or "").strip()
        if not api_key:
            raise VoiceTranscriptionError("OpenAI transcription API key is missing")
        return
    if provider == "whisper":
        _get_whisper_model(settings=settings)
        return
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

    model = _resolve_openai_model_name(settings=settings)
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


def _transcribe_with_whisper(
    *,
    settings: Settings,
    audio_bytes: bytes,
    filename: str,
    content_type: str | None,
) -> str:
    if not audio_bytes:
        raise VoiceTranscriptionError("Audio payload is empty")
    if len(audio_bytes) > int(settings.voice_attachment_max_bytes):
        raise VoiceTranscriptionError("Audio payload exceeds configured maximum size")

    model = _get_whisper_model(settings=settings)
    audio_array = _decode_audio_to_float32_mono(
        audio_bytes=audio_bytes,
        filename=filename,
        content_type=content_type,
    )
    language = str(settings.voice_transcription_language or "").strip() or None
    try:
        segments, _info = model.transcribe(audio_array, language=language)
    except Exception as exc:  # noqa: BLE001
        raise VoiceTranscriptionError(f"Whisper transcription failed: {exc}") from exc

    transcript_parts: list[str] = []
    for segment in segments:
        text = str(getattr(segment, "text", "") or "").strip()
        if text:
            transcript_parts.append(text)
    transcript = " ".join(transcript_parts).strip()
    if not transcript:
        raise VoiceTranscriptionError("Whisper transcription response did not include text")
    return transcript


def _resolve_openai_model_name(*, settings: Settings) -> str:
    configured = str(settings.voice_transcription_model or "").strip()
    if not configured or configured == "base":
        return "gpt-4o-mini-transcribe"
    return configured


def _resolve_whisper_model_name(*, settings: Settings) -> str:
    configured = str(settings.voice_transcription_model or "").strip()
    if not configured or configured == "gpt-4o-mini-transcribe":
        return "base"
    return configured


def _get_whisper_model(*, settings: Settings) -> object:
    model_name = _resolve_whisper_model_name(settings=settings)
    device = str(settings.voice_transcription_device or "").strip() or "auto"
    compute_type = str(settings.voice_transcription_compute_type or "").strip() or "int8"
    cache_key = (model_name, device, compute_type)
    cached_model = _WHISPER_MODELS.get(cache_key)
    if cached_model is not None:
        return cached_model

    with _WHISPER_MODEL_LOCK:
        cached_model = _WHISPER_MODELS.get(cache_key)
        if cached_model is not None:
            return cached_model
        try:
            faster_whisper = __import__("faster_whisper", fromlist=["WhisperModel"])
        except ModuleNotFoundError as exc:
            raise VoiceTranscriptionError(
                "Whisper transcription requires the `faster-whisper` package to be installed."
            ) from exc
        try:
            model = faster_whisper.WhisperModel(
                model_name,
                device=device,
                compute_type=compute_type,
            )
        except Exception as exc:  # noqa: BLE001
            raise VoiceTranscriptionError(f"Whisper model load failed: {exc}") from exc
        _WHISPER_MODELS[cache_key] = model
        return model


def _decode_audio_to_float32_mono(
    *,
    audio_bytes: bytes,
    filename: str,
    content_type: str | None,
) -> object:
    try:
        av = __import__("av")
    except ModuleNotFoundError as exc:
        raise VoiceTranscriptionError("Whisper transcription requires PyAV at runtime.") from exc
    try:
        numpy = __import__("numpy")
    except ModuleNotFoundError as exc:
        raise VoiceTranscriptionError("Whisper transcription requires numpy at runtime.") from exc

    try:
        with av.open(io.BytesIO(audio_bytes), mode="r", metadata_errors="ignore") as container:
            audio_stream = next((stream for stream in container.streams if stream.type == "audio"), None)
            if audio_stream is None:
                raise VoiceTranscriptionError(
                    f"Whisper transcription could not find an audio stream in '{filename}'."
                )
            resampler = av.audio.resampler.AudioResampler(format="s16", layout="mono", rate=16000)
            chunks: list[object] = []
            for frame in container.decode(audio_stream):
                resampled_frames = resampler.resample(frame)
                if resampled_frames is None:
                    continue
                if not isinstance(resampled_frames, list):
                    resampled_frames = [resampled_frames]
                for resampled_frame in resampled_frames:
                    frame_array = resampled_frame.to_ndarray()
                    if getattr(frame_array, "size", 0):
                        chunks.append(frame_array)
    except VoiceTranscriptionError:
        raise
    except Exception as exc:  # noqa: BLE001
        detected_content_type = content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        raise VoiceTranscriptionError(
            f"Whisper audio decode failed for '{filename}' ({detected_content_type}): {exc}"
        ) from exc

    if not chunks:
        raise VoiceTranscriptionError("Whisper audio decode returned no audio samples")

    audio_array = numpy.concatenate(chunks, axis=1).reshape(-1)
    audio_array = audio_array.astype(numpy.float32) / 32768.0
    return audio_array


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
