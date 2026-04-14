from __future__ import annotations

import io
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import numpy as np

from orchestrator.core.config import Settings
from orchestrator.core.voice import tts as tts_module
from orchestrator.core.voice.tts import (
    VoiceReplyError,
    ensure_voice_reply_provider_ready,
    resolve_voice_reply_persona_metadata,
    synthesize_reply_audio,
)


class _FakeAudioTensor:
    def __init__(self, values: np.ndarray) -> None:
        self._values = values

    def detach(self) -> "_FakeAudioTensor":
        return self

    def cpu(self) -> "_FakeAudioTensor":
        return self

    def numpy(self) -> np.ndarray:
        return self._values


class _FakeModel:
    def __init__(self, *, sample_rate: int = 24000) -> None:
        self.sample_rate = sample_rate
        self.loaded_voices: list[str] = []
        self.generated: list[tuple[str, str, bool]] = []

    def get_state_for_audio_prompt(self, voice: str) -> str:
        self.loaded_voices.append(voice)
        return f"state:{voice}"

    def generate_audio(self, state: str, text: str, copy_state: bool = True) -> _FakeAudioTensor:
        self.generated.append((state, text, copy_state))
        return _FakeAudioTensor(np.linspace(-0.25, 0.25, 24000, dtype=np.float32))


class _FakeTTSModel:
    load_calls = 0
    model = _FakeModel()

    @classmethod
    def load_model(cls) -> _FakeModel:
        cls.load_calls += 1
        return cls.model


class _FailingTTSModel:
    @classmethod
    def load_model(cls):  # noqa: ANN206
        raise RuntimeError("weights missing")


class VoiceTtsTests(unittest.TestCase):
    def tearDown(self) -> None:
        tts_module._POCKET_TTS_MODEL = None
        tts_module._POCKET_TTS_VOICE_STATES.clear()
        _FakeTTSModel.load_calls = 0
        _FakeTTSModel.model = _FakeModel()

    def test_synthesize_reply_rejects_disabled_provider(self) -> None:
        settings = Settings(voice_tts_provider="disabled")
        with self.assertRaisesRegex(VoiceReplyError, "disabled"):
            synthesize_reply_audio(settings=settings, text="hello")

    def test_resolve_persona_metadata_prefers_room_config_over_defaults(self) -> None:
        settings = Settings(
            voice_tts_provider="pocket_tts",
            pocket_tts_voice="fantine",
        )

        metadata = resolve_voice_reply_persona_metadata(
            settings=settings,
            persona_id="qa",
            room_config={
                "persona_names": {"qa": "June", "default": "Room Voice"},
                "persona_voices": {"qa": "eponine", "default": "azelma"},
            },
        )

        self.assertEqual(metadata.persona_id, "qa")
        self.assertEqual(metadata.display_name, "June")
        self.assertEqual(metadata.voice, "eponine")

    def test_resolve_persona_metadata_uses_code_defaults_when_no_config_present(self) -> None:
        settings = Settings(voice_tts_provider="pocket_tts")

        metadata = resolve_voice_reply_persona_metadata(
            settings=settings,
            persona_id="security",
            room_config={},
        )

        self.assertEqual(metadata.display_name, "June")
        self.assertTrue(metadata.voice)

    def test_synthesize_reply_uses_library_voice_and_keeps_native_speed(self) -> None:
        settings = Settings(voice_tts_provider="pocket_tts")
        runtime = {
            "TTSModel": _FakeTTSModel,
            "numpy": np,
            "signal": __import__("scipy.signal", fromlist=["resample"]),
        }

        with (
            patch("orchestrator.core.voice.tts._load_pocket_tts_runtime", return_value=runtime),
            patch(
                "orchestrator.core.voice.tts._resolve_pocket_tts_audio_prompt_source",
                side_effect=lambda voice, **_: Path(f"/tmp/{voice}.safetensors"),
            ),
        ):
            audio = synthesize_reply_audio(
                settings=settings,
                text=" hello ",
                persona_id="architect",
                room_config={"persona_voices": {"architect": "marius"}},
            )

        self.assertEqual(audio.content_type, "audio/wav")
        self.assertEqual(audio.filename, "voice_reply.wav")
        self.assertEqual(_FakeTTSModel.load_calls, 1)
        self.assertEqual(len(_FakeTTSModel.model.loaded_voices), 1)
        self.assertTrue(str(_FakeTTSModel.model.loaded_voices[0]).endswith("/marius.safetensors"))
        self.assertEqual(len(_FakeTTSModel.model.generated), 1)
        generated_state, generated_text, copy_state = _FakeTTSModel.model.generated[0]
        self.assertTrue(str(generated_state).endswith("/marius.safetensors"))
        self.assertEqual(generated_text, "hello")
        self.assertTrue(copy_state)

        with wave.open(io.BytesIO(audio.audio_bytes), "rb") as wav_file:
            self.assertEqual(wav_file.getframerate(), 24000)
            self.assertEqual(wav_file.getnchannels(), 1)
            frame_count = wav_file.getnframes()
        self.assertEqual(frame_count, 28800)

    def test_synthesize_reply_caches_model_and_voice_state(self) -> None:
        settings = Settings(voice_tts_provider="pocket_tts")
        runtime = {
            "TTSModel": _FakeTTSModel,
            "numpy": np,
            "signal": __import__("scipy.signal", fromlist=["resample"]),
        }

        with (
            patch("orchestrator.core.voice.tts._load_pocket_tts_runtime", return_value=runtime),
            patch(
                "orchestrator.core.voice.tts._resolve_pocket_tts_audio_prompt_source",
                side_effect=lambda voice, **_: Path(f"/tmp/{voice}.safetensors"),
            ),
        ):
            synthesize_reply_audio(
                settings=settings,
                text="first",
                persona_id="pm",
                room_config={"persona_voices": {"pm": "alba"}},
            )
            synthesize_reply_audio(
                settings=settings,
                text="second",
                persona_id="pm",
                room_config={"persona_voices": {"pm": "alba"}},
            )

        self.assertEqual(_FakeTTSModel.load_calls, 1)
        self.assertEqual(len(_FakeTTSModel.model.loaded_voices), 1)
        self.assertTrue(str(_FakeTTSModel.model.loaded_voices[0]).endswith("/alba.safetensors"))
        self.assertEqual(len(_FakeTTSModel.model.generated), 2)
        first_state, first_text, first_copy = _FakeTTSModel.model.generated[0]
        second_state, second_text, second_copy = _FakeTTSModel.model.generated[1]
        self.assertTrue(str(first_state).endswith("/alba.safetensors"))
        self.assertTrue(str(second_state).endswith("/alba.safetensors"))
        self.assertEqual((first_text, first_copy), ("first", True))
        self.assertEqual((second_text, second_copy), ("second", True))

    def test_ensure_voice_reply_provider_ready_prewarms_predefined_voices(self) -> None:
        settings = Settings(
            voice_tts_provider="pocket_tts",
            pocket_tts_voice="jean",
        )
        runtime = {
            "TTSModel": _FakeTTSModel,
            "numpy": np,
            "signal": __import__("scipy.signal", fromlist=["resample"]),
        }

        with (
            patch("orchestrator.core.voice.tts._load_pocket_tts_runtime", return_value=runtime),
            patch(
                "orchestrator.core.voice.tts.list_predefined_pocket_tts_voices",
                return_value=("alba", "jean"),
            ),
            patch(
                "orchestrator.core.voice.tts._resolve_pocket_tts_audio_prompt_source",
                side_effect=lambda voice, **_: Path(f"/tmp/{voice}.safetensors"),
            ),
        ):
            warmed_voice_ids = ensure_voice_reply_provider_ready(settings=settings)

        self.assertEqual(warmed_voice_ids, ["alba", "jean"])
        self.assertEqual(_FakeTTSModel.load_calls, 1)
        self.assertEqual(len(_FakeTTSModel.model.loaded_voices), 2)
        self.assertTrue(str(_FakeTTSModel.model.loaded_voices[0]).endswith("/alba.safetensors"))
        self.assertTrue(str(_FakeTTSModel.model.loaded_voices[1]).endswith("/jean.safetensors"))

    def test_predefined_voice_uses_cached_hf_asset_before_network(self) -> None:
        huggingface_hub = type("Hub", (), {})()
        recorded_calls: list[dict[str, object]] = []

        def _fake_hf_hub_download(**kwargs):  # noqa: ANN003
            recorded_calls.append(kwargs)
            return "/tmp/alba.safetensors"

        huggingface_hub.hf_hub_download = _fake_hf_hub_download
        pocket_tts_utils = type("Utils", (), {"PREDEFINED_VOICES": {"alba": "hf://repo/name/path/alba.safetensors@rev"}})()

        runtime = {
            "TTSModel": _FakeTTSModel,
            "numpy": np,
            "signal": __import__("scipy.signal", fromlist=["resample"]),
        }

        with (
            patch("orchestrator.core.voice.tts._load_pocket_tts_runtime", return_value=runtime),
            patch("orchestrator.core.voice.tts._load_pocket_tts_utils_module", return_value=pocket_tts_utils),
            patch("orchestrator.core.voice.tts.import_module", return_value=huggingface_hub),
        ):
            synthesize_reply_audio(
                settings=Settings(voice_tts_provider="pocket_tts"),
                text="hello",
                persona_id="pm",
                room_config={"persona_voices": {"pm": "alba"}},
            )

        self.assertEqual(len(recorded_calls), 1)
        self.assertTrue(recorded_calls[0]["local_files_only"])
        self.assertEqual(_FakeTTSModel.model.loaded_voices, [Path("/tmp/alba.safetensors")])

    def test_predefined_voice_runtime_fails_when_hf_asset_not_cached(self) -> None:
        huggingface_hub = type("Hub", (), {})()

        def _fake_hf_hub_download(**kwargs):  # noqa: ANN003
            raise RuntimeError(f"missing cache local_only={kwargs.get('local_files_only')}")

        huggingface_hub.hf_hub_download = _fake_hf_hub_download
        pocket_tts_utils = type("Utils", (), {"PREDEFINED_VOICES": {"alba": "hf://repo/name/path/alba.safetensors@rev"}})()

        runtime = {
            "TTSModel": _FakeTTSModel,
            "numpy": np,
            "signal": __import__("scipy.signal", fromlist=["resample"]),
        }

        with (
            patch("orchestrator.core.voice.tts._load_pocket_tts_runtime", return_value=runtime),
            patch("orchestrator.core.voice.tts._load_pocket_tts_utils_module", return_value=pocket_tts_utils),
            patch("orchestrator.core.voice.tts.import_module", return_value=huggingface_hub),
        ):
            with self.assertRaisesRegex(VoiceReplyError, "not cached locally"):
                synthesize_reply_audio(
                    settings=Settings(voice_tts_provider="pocket_tts"),
                    text="hello",
                    persona_id="pm",
                    room_config={"persona_voices": {"pm": "alba"}},
                )

    def test_synthesize_reply_surfaces_missing_package(self) -> None:
        settings = Settings(voice_tts_provider="pocket_tts")
        with patch(
            "orchestrator.core.voice.tts._load_pocket_tts_runtime",
            side_effect=VoiceReplyError("Pocket TTS Python package is not installed."),
        ):
            with self.assertRaisesRegex(VoiceReplyError, "not installed"):
                synthesize_reply_audio(settings=settings, text="hello", persona_id="pm")

    def test_synthesize_reply_surfaces_model_load_failure(self) -> None:
        settings = Settings(voice_tts_provider="pocket_tts")
        runtime = {
            "TTSModel": _FailingTTSModel,
            "numpy": np,
            "signal": __import__("scipy.signal", fromlist=["resample"]),
        }
        with patch("orchestrator.core.voice.tts._load_pocket_tts_runtime", return_value=runtime):
            with self.assertRaisesRegex(VoiceReplyError, "model load failed"):
                synthesize_reply_audio(settings=settings, text="hello", persona_id="pm")

    def test_synthesize_reply_rejects_empty_text(self) -> None:
        settings = Settings(voice_tts_provider="pocket_tts")
        with self.assertRaisesRegex(VoiceReplyError, "cannot be empty"):
            synthesize_reply_audio(settings=settings, text="   ", persona_id="pm")


if __name__ == "__main__":
    unittest.main()
