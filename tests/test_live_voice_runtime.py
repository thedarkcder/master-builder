from __future__ import annotations

import unittest
from datetime import datetime, timezone

from orchestrator.core.discord.live_voice_runtime import LiveVoiceRuntimeService
from orchestrator.core.discord.live_voice_session import (
    LiveVoiceCallbacks,
    LiveVoiceRoomBinding,
    LiveVoiceSessionState,
)


class _Recorder:
    def __init__(self) -> None:
        self.state_changes: list[tuple[str, str]] = []
        self.turns: list[tuple[int, bytes, str]] = []
        self.discarded: list[str] = []

    def on_session_state_changed(self, *, previous, current) -> None:  # noqa: ANN001
        self.state_changes.append((previous.state.value, current.state.value))

    def on_turn_finalized(self, *, session, turn) -> None:  # noqa: ANN001
        self.turns.append((turn.turn_index, turn.audio_bytes, session.state.value))

    def on_turn_discarded(self, *, session, reason) -> None:  # noqa: ANN001
        self.discarded.append(reason)


class LiveVoiceRuntimeTests(unittest.TestCase):
    def test_runtime_registers_room_and_finalizes_turn_on_poll(self) -> None:
        recorder = _Recorder()
        runtime = LiveVoiceRuntimeService(
            callbacks=LiveVoiceCallbacks(
                on_session_state_changed=recorder.on_session_state_changed,
                on_turn_finalized=recorder.on_turn_finalized,
                on_turn_discarded=recorder.on_turn_discarded,
            ),
            clock=lambda: datetime(2026, 3, 19, 10, 0, tzinfo=timezone.utc),
        )
        binding = LiveVoiceRoomBinding(guild_id="guild-1", voice_channel_id="voice-1", text_channel_id="text-1")

        session = runtime.register_room(binding=binding, bot_user_id="bot-1")
        self.assertEqual(session.state, LiveVoiceSessionState.DISCONNECTED)
        runtime.join_room(binding=binding, human_member_count=1, now=datetime(2026, 3, 19, 10, 0, tzinfo=timezone.utc))

        runtime.ingest_audio(
            binding=binding,
            user_id="user-1",
            audio_bytes=b"a",
            received_at=datetime(2026, 3, 19, 10, 1, tzinfo=timezone.utc),
        )
        runtime.ingest_audio(
            binding=binding,
            user_id="user-1",
            audio_bytes=b"b",
            received_at=datetime(2026, 3, 19, 10, 1, 1, tzinfo=timezone.utc),
        )

        self.assertEqual(runtime.get_session(binding=binding).state, LiveVoiceSessionState.PROCESSING)
        turns = runtime.poll(now=datetime(2026, 3, 19, 10, 1, 3, tzinfo=timezone.utc))
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0].audio_bytes, b"ab")
        self.assertEqual(turns[0].finalization_reason, "silence")
        self.assertEqual(runtime.get_session(binding=binding).state, LiveVoiceSessionState.LISTENING)
        self.assertEqual(runtime.get_session(binding=binding).turn_index, 1)
        self.assertTrue(any(change == ("disconnected", "joining") for change in recorder.state_changes))
        self.assertTrue(any(change == ("joining", "listening") for change in recorder.state_changes))
        self.assertTrue(any(change == ("listening", "processing") for change in recorder.state_changes))
        self.assertTrue(any(change == ("processing", "listening") for change in recorder.state_changes))
        self.assertEqual(recorder.turns, [(1, b"ab", "listening")])

    def test_runtime_blocks_bot_audio_and_supports_speaking_guard(self) -> None:
        runtime = LiveVoiceRuntimeService(clock=lambda: datetime(2026, 3, 19, 10, 0, tzinfo=timezone.utc))
        binding = LiveVoiceRoomBinding(guild_id="guild-1", voice_channel_id="voice-1")
        runtime.register_room(binding=binding, bot_user_id="bot-1")
        runtime.join_room(binding=binding, human_member_count=1, now=datetime(2026, 3, 19, 10, 0, tzinfo=timezone.utc))

        runtime.mark_bot_speaking(binding=binding, speaking=True, now=datetime(2026, 3, 19, 10, 0, 1, tzinfo=timezone.utc))
        self.assertIsNone(
            runtime.ingest_audio(
                binding=binding,
                user_id="bot-1",
                audio_bytes=b"bot",
                is_bot_audio=True,
                received_at=datetime(2026, 3, 19, 10, 0, 2, tzinfo=timezone.utc),
            )
        )
        self.assertIsNone(
            runtime.ingest_audio(
                binding=binding,
                user_id="user-1",
                audio_bytes=b"user",
                received_at=datetime(2026, 3, 19, 10, 0, 2, tzinfo=timezone.utc),
            )
        )
        self.assertEqual(runtime.get_session(binding=binding).state, LiveVoiceSessionState.SPEAKING)

        runtime.mark_bot_speaking(binding=binding, speaking=False, now=datetime(2026, 3, 19, 10, 0, 3, tzinfo=timezone.utc))
        self.assertEqual(runtime.get_session(binding=binding).state, LiveVoiceSessionState.LISTENING)

    def test_runtime_evaluates_join_and_leave_decisions(self) -> None:
        runtime = LiveVoiceRuntimeService(clock=lambda: datetime(2026, 3, 19, 10, 0, tzinfo=timezone.utc))
        binding = LiveVoiceRoomBinding(guild_id="guild-1", voice_channel_id="voice-1")
        runtime.register_room(binding=binding, bot_user_id="bot-1")

        join_decision = runtime.evaluate_join(binding=binding, human_member_count=0)
        self.assertFalse(join_decision.should_transition)

        runtime.join_room(binding=binding, human_member_count=1, now=datetime(2026, 3, 19, 10, 0, 1, tzinfo=timezone.utc))
        leave_decision = runtime.evaluate_leave(binding=binding, human_member_count=0)
        self.assertTrue(leave_decision.should_transition)

        runtime.ingest_audio(
            binding=binding,
            user_id="user-1",
            audio_bytes=b"a",
            received_at=datetime(2026, 3, 19, 10, 0, 2, tzinfo=timezone.utc),
        )
        self.assertFalse(runtime.evaluate_leave(binding=binding, human_member_count=0).should_transition)


if __name__ == "__main__":
    unittest.main()
