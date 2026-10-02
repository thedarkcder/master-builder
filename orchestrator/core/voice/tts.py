from __future__ import annotations

import io
import threading
import wave
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any
from typing import Sequence

from orchestrator.core.config import Settings
from orchestrator.core.discord.personas import (
    get_voice_room_persona_definition,
    list_voice_room_personas,
)

if TYPE_CHECKING:
    from collections.abc import Iterable


class VoiceReplyError(RuntimeError):
    pass


@dataclass(frozen=True)
class VoiceReplyAudio:
    audio_bytes: bytes
    filename: str
    content_type: str


@dataclass(frozen=True)
class VoiceReplyPersonaMetadata:
    persona_id: str | None
    display_name: str
    voice: str


_PERSONA_NAME_KEYS = (
    "persona_names",
    "voice_room_persona_names",
    "room_persona_names",
    "pm_room_persona_names",
)
_PERSONA_VOICE_KEYS = (
    "persona_voices",
    "voice_room_persona_voices",
    "room_persona_voices",
    "pm_room_persona_voices",
)
_DEFAULT_PLAYBACK_SPEED = 1.0
_TRAILING_SILENCE_SECONDS = 0.2
_MODEL_LOCK = threading.RLock()
_POCKET_TTS_MODEL: Any | None = None
_POCKET_TTS_VOICE_STATES: dict[str, Any] = {}


def synthesize_reply_audio(
    *,
    settings: Settings,
    text: str,
    persona_id: str | None = None,
    room_config: dict | None = None,
) -> VoiceReplyAudio:
    provider = str(settings.voice_tts_provider or "").strip().lower()
    if provider in {"", "disabled"}:
        raise VoiceReplyError("Voice reply is disabled")
    if provider == "pocket_tts":
        persona_metadata = resolve_voice_reply_persona_metadata(
            settings=settings,
            persona_id=persona_id,
            room_config=room_config,
        )
        return _synthesize_with_pocket_tts(
            text=text,
            voice=persona_metadata.voice,
        )
    raise VoiceReplyError(f"Unsupported voice reply provider '{provider}'")


def ensure_voice_reply_provider_ready(
    *,
    settings: Settings,
    voices: Sequence[str] | None = None,
    allow_download: bool = False,
) -> list[str]:
    provider = str(settings.voice_tts_provider or "").strip().lower()
    if provider in {"", "disabled"}:
        raise VoiceReplyError("Voice reply is disabled")
    if provider != "pocket_tts":
        raise VoiceReplyError(f"Unsupported voice reply provider '{provider}'")

    normalized_voices = _resolve_prewarm_voice_ids(settings=settings, voices=voices)
    with _MODEL_LOCK:
        model = _get_pocket_tts_model()
        for voice in normalized_voices:
            _get_pocket_tts_voice_state(
                model=model, voice=voice, allow_download=allow_download
            )
    return normalized_voices


def list_predefined_pocket_tts_voices() -> tuple[str, ...]:
    predefined = getattr(_load_pocket_tts_utils_module(), "PREDEFINED_VOICES", None)
    if not isinstance(predefined, dict):
        raise VoiceReplyError(
            "Pocket TTS predefined voices are unavailable at runtime."
        )

    voice_ids = {
        str(voice_id or "").strip().lower()
        for voice_id in predefined
        if str(voice_id or "").strip()
    }
    return tuple(sorted(voice_ids))


def resolve_voice_reply_persona_metadata(
    *,
    settings: Settings,
    persona_id: str | None = None,
    room_config: dict | None = None,
) -> VoiceReplyPersonaMetadata:
    normalized_persona_id = _normalize_persona_id(persona_id)
    config = dict(room_config or {})
    persona_names = _merged_string_map(config, _PERSONA_NAME_KEYS)
    persona_voices = _merged_string_map(config, _PERSONA_VOICE_KEYS)

    display_name = persona_names.get(normalized_persona_id or "", "").strip()
    if not display_name:
        display_name = persona_names.get("default", "").strip()
    if not display_name:
        display_name = _default_persona_display_name(normalized_persona_id)

    voice = persona_voices.get(normalized_persona_id or "", "").strip()
    if not voice:
        voice = persona_voices.get("default", "").strip()
    if not voice:
        voice = _default_persona_voice(normalized_persona_id)
    if not voice:
        voice = str(settings.pocket_tts_voice or "").strip()
    if not voice:
        raise VoiceReplyError("Pocket TTS voice is missing")

    return VoiceReplyPersonaMetadata(
        persona_id=normalized_persona_id,
        display_name=display_name,
        voice=voice,
    )


def _synthesize_with_pocket_tts(*, text: str, voice: str) -> VoiceReplyAudio:
    normalized_text = text.strip()
    if not normalized_text:
        raise VoiceReplyError("Voice reply text cannot be empty")
    resolved_voice = str(voice or "").strip().lower()
    if not resolved_voice:
        raise VoiceReplyError("Pocket TTS voice is missing")

    with _MODEL_LOCK:
        model = _get_pocket_tts_model()
        voice_state = _get_pocket_tts_voice_state(model=model, voice=resolved_voice)
        try:
            audio_tensor = model.generate_audio(
                voice_state,
                normalized_text,
                copy_state=True,
            )
        except Exception as exc:  # noqa: BLE001
            raise VoiceReplyError(f"Pocket TTS synthesis failed: {exc}") from exc
        sample_rate = int(getattr(model, "sample_rate", 24000) or 24000)

    audio_bytes = _audio_tensor_to_wav_bytes(
        audio_tensor=audio_tensor,
        sample_rate=sample_rate,
        playback_speed=_DEFAULT_PLAYBACK_SPEED,
    )
    return VoiceReplyAudio(
        audio_bytes=audio_bytes,
        filename="voice_reply.wav",
        content_type="audio/wav",
    )


def _get_pocket_tts_model() -> Any:
    global _POCKET_TTS_MODEL
    if _POCKET_TTS_MODEL is not None:
        return _POCKET_TTS_MODEL
    runtime = _load_pocket_tts_runtime()
    try:
        _POCKET_TTS_MODEL = runtime["TTSModel"].load_model()
    except Exception as exc:  # noqa: BLE001
        raise VoiceReplyError(f"Pocket TTS model load failed: {exc}") from exc
    return _POCKET_TTS_MODEL


def _get_pocket_tts_voice_state(
    *, model: Any, voice: str, allow_download: bool = False
) -> Any:
    cached_state = _POCKET_TTS_VOICE_STATES.get(voice)
    if cached_state is not None:
        return cached_state
    try:
        cached_state = model.get_state_for_audio_prompt(
            _resolve_pocket_tts_audio_prompt_source(
                voice, allow_download=allow_download
            )
        )
    except Exception as exc:  # noqa: BLE001
        raise VoiceReplyError(
            f"Pocket TTS voice '{voice}' failed to load: {exc}"
        ) from exc
    _POCKET_TTS_VOICE_STATES[voice] = cached_state
    return cached_state


def _load_pocket_tts_runtime() -> dict[str, Any]:
    try:
        pocket_tts = import_module("pocket_tts")
    except ModuleNotFoundError as exc:
        raise VoiceReplyError(
            "Pocket TTS Python package is not installed. Install it with `pip install pocket-tts`."
        ) from exc

    try:
        numpy = import_module("numpy")
    except ModuleNotFoundError as exc:
        raise VoiceReplyError("Pocket TTS requires numpy at runtime.") from exc

    try:
        scipy_signal = import_module("scipy.signal")
    except ModuleNotFoundError as exc:
        raise VoiceReplyError("Pocket TTS requires scipy at runtime.") from exc

    return {
        "TTSModel": getattr(pocket_tts, "TTSModel"),
        "numpy": numpy,
        "signal": scipy_signal,
    }


def _load_pocket_tts_utils_module() -> Any:
    try:
        return import_module("pocket_tts.utils.utils")
    except ModuleNotFoundError as exc:
        raise VoiceReplyError(
            "Pocket TTS Python package is not installed. Install it with `pip install pocket-tts`."
        ) from exc


def _resolve_pocket_tts_audio_prompt_source(
    voice: str, *, allow_download: bool = False
) -> str | Path:
    normalized_voice = str(voice or "").strip().lower()
    pocket_tts_utils = _load_pocket_tts_utils_module()
    predefined = getattr(pocket_tts_utils, "PREDEFINED_VOICES", None)
    if isinstance(predefined, dict):
        predefined_source = predefined.get(normalized_voice)
        if predefined_source:
            return _resolve_cached_audio_prompt_path(
                str(predefined_source), allow_download=allow_download
            )
    return normalized_voice


def _resolve_cached_audio_prompt_path(
    source: str, *, allow_download: bool = False
) -> Path:
    normalized_source = str(source or "").strip()
    if not normalized_source:
        raise VoiceReplyError("Pocket TTS audio prompt source is missing")
    if normalized_source.startswith("hf://"):
        return _download_hf_hub_file(normalized_source, allow_download=allow_download)
    pocket_tts_utils = _load_pocket_tts_utils_module()
    return Path(pocket_tts_utils.download_if_necessary(normalized_source))


def _download_hf_hub_file(source: str, *, allow_download: bool = False) -> Path:
    normalized_source = str(source or "").strip()
    if not normalized_source.startswith("hf://"):
        raise VoiceReplyError("Pocket TTS HF source is invalid")
    trimmed_source = normalized_source.removeprefix("hf://")
    path_parts = trimmed_source.split("/")
    if len(path_parts) < 3:
        raise VoiceReplyError(f"Pocket TTS HF source '{source}' is invalid")
    repo_id = "/".join(path_parts[:2])
    filename = "/".join(path_parts[2:])
    revision = None
    if "@" in filename:
        filename, revision = filename.rsplit("@", 1)

    try:
        huggingface_hub = import_module("huggingface_hub")
    except ModuleNotFoundError as exc:
        raise VoiceReplyError(
            "Pocket TTS requires huggingface_hub at runtime."
        ) from exc

    try:
        cached_path = huggingface_hub.hf_hub_download(
            repo_id=repo_id,
            filename=filename,
            revision=revision,
            local_files_only=True,
        )
    except Exception:  # noqa: BLE001
        if not allow_download:
            raise VoiceReplyError(
                f"Pocket TTS voice asset '{source}' is not cached locally."
            ) from None
        try:
            cached_path = huggingface_hub.hf_hub_download(
                repo_id=repo_id,
                filename=filename,
                revision=revision,
                local_files_only=False,
            )
        except Exception as exc:  # noqa: BLE001
            raise VoiceReplyError(
                f"Pocket TTS voice asset '{source}' failed to cache: {exc}"
            ) from exc
    return Path(cached_path)


def _resolve_prewarm_voice_ids(
    *, settings: Settings, voices: Sequence[str] | None
) -> list[str]:
    requested_voices: list[str] = []
    if voices is not None:
        requested_voices.extend(_normalize_voice_ids(voices))
    else:
        requested_voices.extend(_default_voice_reply_voice_ids())
        configured_voice = str(settings.pocket_tts_voice or "").strip().lower()
        if configured_voice:
            requested_voices.append(configured_voice)
    return _unique_preserving_order(requested_voices)


def _normalize_voice_ids(voices: "Iterable[str]") -> list[str]:
    normalized: list[str] = []
    for voice in voices:
        normalized_voice = str(voice or "").strip().lower()
        if normalized_voice:
            normalized.append(normalized_voice)
    return normalized


def _unique_preserving_order(values: "Iterable[str]") -> list[str]:
    unique_values: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique_values.append(value)
    return unique_values


def _audio_tensor_to_wav_bytes(
    *,
    audio_tensor: Any,
    sample_rate: int,
    playback_speed: float,
) -> bytes:
    runtime = _load_pocket_tts_runtime()
    numpy = runtime["numpy"]
    signal = runtime["signal"]

    if hasattr(audio_tensor, "detach"):
        audio_array = audio_tensor.detach().cpu().numpy()
    else:
        audio_array = numpy.asarray(audio_tensor)

    audio_array = numpy.asarray(audio_array, dtype=numpy.float32)
    if audio_array.ndim == 0:
        raise VoiceReplyError("Pocket TTS returned an empty audio tensor")
    if audio_array.ndim == 1:
        audio_array = audio_array[numpy.newaxis, :]
    elif audio_array.ndim > 2:
        audio_array = numpy.reshape(audio_array, (audio_array.shape[0], -1))

    if audio_array.shape[-1] <= 0:
        raise VoiceReplyError("Pocket TTS returned an empty audio tensor")

    if playback_speed > 0 and playback_speed != 1.0:
        target_samples = max(1, int(round(audio_array.shape[-1] / playback_speed)))
        audio_array = signal.resample(audio_array, target_samples, axis=-1)

    pcm = numpy.clip(audio_array, -1.0, 1.0)
    pcm = (pcm * 32767.0).astype(numpy.int16)
    silence_samples = max(0, int(round(sample_rate * _TRAILING_SILENCE_SECONDS)))
    if silence_samples:
        silence = numpy.zeros((pcm.shape[0], silence_samples), dtype=numpy.int16)
        pcm = numpy.concatenate((pcm, silence), axis=-1)
    interleaved = pcm.T.reshape(-1)

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(int(pcm.shape[0]))
        wav_file.setsampwidth(2)
        wav_file.setframerate(int(sample_rate))
        wav_file.writeframes(interleaved.tobytes())
    return buffer.getvalue()


def _merged_string_map(config: dict, keys: tuple[str, ...]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for key in keys:
        value = config.get(key)
        if not isinstance(value, dict):
            continue
        merged.update(_normalize_string_map(value))
    return merged


def _normalize_string_map(raw: dict | None) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, str] = {}
    for key, value in raw.items():
        normalized_key = str(key).strip().lower()
        normalized_value = str(value).strip()
        if not normalized_key or not normalized_value:
            continue
        normalized[normalized_key] = normalized_value
    return normalized


def _normalize_persona_id(persona_id: str | None) -> str | None:
    normalized = str(persona_id or "").strip().lower()
    return normalized or None


def _default_persona_display_name(persona_id: str | None) -> str:
    if not persona_id:
        return "Voice Reply"
    try:
        return get_voice_room_persona_definition(persona_id).default_display_name
    except Exception:  # noqa: BLE001
        return persona_id.replace("_", " ").strip().title()


def _default_persona_voice(persona_id: str | None) -> str:
    if not persona_id:
        return ""
    try:
        return get_voice_room_persona_definition(persona_id).default_voice_id
    except Exception:  # noqa: BLE001
        return ""


def _default_voice_reply_voice_ids() -> list[str]:
    return _unique_preserving_order(
        _normalize_voice_ids(
            persona.get("voice_id") for persona in list_voice_room_personas()
        )
    )
