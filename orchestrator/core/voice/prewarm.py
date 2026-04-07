from __future__ import annotations

from dataclasses import dataclass

from orchestrator.core.config import Settings
from orchestrator.core.voice.transcription import ensure_transcription_provider_ready
from orchestrator.core.voice.tts import ensure_voice_reply_provider_ready


@dataclass(frozen=True)
class VoiceDependencyPrewarmResult:
    voice_stt_provider: str
    voice_tts_provider: str
    transcription_ready: bool
    prewarmed_voice_ids: tuple[str, ...]


def prewarm_voice_dependencies(*, settings: Settings) -> VoiceDependencyPrewarmResult:
    voice_stt_provider = str(settings.voice_stt_provider or "").strip().lower()
    voice_tts_provider = str(settings.voice_tts_provider or "").strip().lower()
    transcription_ready = False
    if voice_stt_provider in {"openai", "whisper"}:
        ensure_transcription_provider_ready(settings=settings)
        transcription_ready = True

    prewarmed_voice_ids: tuple[str, ...] = ()
    if voice_tts_provider == "pocket_tts":
        prewarmed_voice_ids = tuple(ensure_voice_reply_provider_ready(settings=settings))

    return VoiceDependencyPrewarmResult(
        voice_stt_provider=voice_stt_provider or "disabled",
        voice_tts_provider=voice_tts_provider or "disabled",
        transcription_ready=transcription_ready,
        prewarmed_voice_ids=prewarmed_voice_ids,
    )
