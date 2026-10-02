from __future__ import annotations

import ctypes.util
import subprocess
import wave
from io import BytesIO
from typing import Any


class LiveVoiceAudioError(RuntimeError):
    pass


def ensure_opus_library_ready() -> None:
    try:
        from discord import opus
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise LiveVoiceAudioError(
            "Live voice audio decoding requires py-cord to be installed."
        ) from exc

    if opus.is_loaded():
        return

    library_name = ctypes.util.find_library("opus")
    if not library_name:
        raise LiveVoiceAudioError(
            "Live voice audio decoding requires the system Opus library to be installed."
        )

    try:
        opus.load_opus(library_name)
    except Exception as exc:  # noqa: BLE001
        raise LiveVoiceAudioError(
            f"Live voice audio decoding failed to load the Opus library '{library_name}'."
        ) from exc


def ensure_opus_decoder_ready() -> None:
    ensure_opus_library_ready()


def build_opus_decoder() -> Any:
    ensure_opus_library_ready()
    from discord.opus import Decoder

    return Decoder()


def build_opus_encoder() -> Any:
    ensure_opus_library_ready()
    from discord.opus import Encoder

    return Encoder()


def decode_opus_packets_to_pcm(
    *, packets: list[bytes], decoder: Any
) -> tuple[bytes, int, int]:
    if not packets:
        return b"", 48_000, 2

    pcm_chunks: list[bytes] = []
    for packet in packets:
        if not packet:
            continue
        try:
            pcm = decoder.decode(packet)
        except Exception as exc:  # noqa: BLE001
            raise LiveVoiceAudioError(
                "Live voice input audio contained a corrupt Opus frame."
            ) from exc
        if pcm:
            pcm_chunks.append(pcm)

    return b"".join(pcm_chunks), 48_000, 2


def encode_wav_to_opus_frames(*, wav_bytes: bytes) -> list[bytes]:
    pcm_bytes = _normalize_wav_to_pcm_48k_stereo(wav_bytes=wav_bytes)
    if not pcm_bytes:
        return []

    encoder = build_opus_encoder()
    frame_size = int(getattr(encoder, "FRAME_SIZE", 3840) or 3840)
    samples_per_frame = int(getattr(encoder, "SAMPLES_PER_FRAME", 960) or 960)
    if frame_size <= 0 or samples_per_frame <= 0:
        raise LiveVoiceAudioError("Opus encoder reported invalid frame sizing.")

    remainder = len(pcm_bytes) % frame_size
    if remainder:
        pcm_bytes += b"\x00" * (frame_size - remainder)

    frames: list[bytes] = []
    for offset in range(0, len(pcm_bytes), frame_size):
        chunk = pcm_bytes[offset : offset + frame_size]
        if not chunk:
            continue
        try:
            frames.append(encoder.encode(chunk, samples_per_frame))
        except Exception as exc:  # noqa: BLE001
            raise LiveVoiceAudioError(
                "Live voice reply audio failed to encode to Opus."
            ) from exc
    return frames


def _normalize_wav_to_pcm_48k_stereo(*, wav_bytes: bytes) -> bytes:
    if not wav_bytes:
        raise LiveVoiceAudioError("Live voice reply audio payload cannot be empty.")

    try:
        result = subprocess.run(  # noqa: S603
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                "pipe:0",
                "-f",
                "s16le",
                "-acodec",
                "pcm_s16le",
                "-ar",
                "48000",
                "-ac",
                "2",
                "pipe:1",
            ],
            input=wav_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:  # pragma: no cover
        raise LiveVoiceAudioError(
            "ffmpeg is required for live voice reply encoding."
        ) from exc

    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="ignore").strip()
        detail = f": {stderr}" if stderr else ""
        raise LiveVoiceAudioError(
            f"ffmpeg failed to normalize live voice reply audio{detail}"
        )

    return bytes(result.stdout)


def wav_duration_seconds(wav_bytes: bytes) -> float:
    with wave.open(BytesIO(wav_bytes), "rb") as wav_file:
        frame_count = wav_file.getnframes()
        sample_rate = wav_file.getframerate()
    if sample_rate <= 0:
        raise LiveVoiceAudioError("WAV sample rate must be positive.")
    return frame_count / sample_rate
