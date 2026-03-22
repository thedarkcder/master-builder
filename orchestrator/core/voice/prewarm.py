from __future__ import annotations

from dataclasses import dataclass

from orchestrator.core.config import Settings
from orchestrator.core.voice.transcription import ensure_transcription_provider_ready
from orchestrator.core.voice.tts import ensure_voice_reply_provider_ready


@dataclass(frozen=True)
class VoiceDependencyPrewarmResult:
    transcription_provider: str
    transcription_ready: bool
    voice_reply_provider: str
    prewarmed_voice_ids: tuple[str, ...]


def prewarm_voice_dependencies(*, settings: Settings) -> VoiceDependencyPrewarmResult:
    transcription_provider = str(settings.voice_transcription_provider or "").strip().lower()
    transcription_ready = False
    if transcription_provider not in {"", "disabled"}:
        ensure_transcription_provider_ready(settings=settings)
        transcription_ready = True

    voice_reply_provider = str(settings.voice_reply_provider or "").strip().lower()
    prewarmed_voice_ids: tuple[str, ...] = ()
    if voice_reply_provider not in {"", "disabled"}:
        prewarmed_voice_ids = tuple(ensure_voice_reply_provider_ready(settings=settings))

    return VoiceDependencyPrewarmResult(
        transcription_provider=transcription_provider or "disabled",
        transcription_ready=transcription_ready,
        voice_reply_provider=voice_reply_provider or "disabled",
        prewarmed_voice_ids=prewarmed_voice_ids,
    )
