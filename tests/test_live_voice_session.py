from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from orchestrator.core.discord.live_voice_session import (
    LiveVoiceRoomBinding,
    LiveVoiceSessionManager,
    LiveVoiceSessionState,
    LiveVoiceTurnSegmenter,
)


class LiveVoiceSessionTests(unittest.TestCase):
    def test_session_manager_decides_join_and_leave_from_presence(self) -> None:
        binding = LiveVoiceRoomBinding(guild_id="guild-1", voice_channel_id="voice-1")
        manager = LiveVoiceSessionManager()
        session = manager.create_session(binding=binding, bot_user_id="bot-1")

        self.assertFalse(
            manager.decide_join(session=session, human_member_count=0).should_transition
        )
        self.assertTrue(
            manager.decide_join(session=session, human_member_count=1).should_transition
        )

        session = manager.begin_joining(
            session=session, now=datetime(2026, 3, 19, 10, 0, tzinfo=timezone.utc)
        )
        session = manager.mark_joined(
            session=session,
            human_member_count=2,
            now=datetime(2026, 3, 19, 10, 0, 5, tzinfo=timezone.utc),
        )
        self.assertEqual(session.state, LiveVoiceSessionState.LISTENING)
        self.assertFalse(
            manager.decide_leave(
                session=session, human_member_count=1
            ).should_transition
        )
        self.assertTrue(
            manager.decide_leave(
                session=session, human_member_count=0
            ).should_transition
        )

    def test_session_manager_blocks_audio_while_bot_speaking(self) -> None:
        binding = LiveVoiceRoomBinding(guild_id="guild-1", voice_channel_id="voice-1")
        manager = LiveVoiceSessionManager()
        session = manager.create_session(binding=binding, bot_user_id="bot-1")
        session = manager.mark_joined(
            session=manager.begin_joining(
                session=session, now=datetime(2026, 3, 19, 10, 0, tzinfo=timezone.utc)
            ),
            human_member_count=1,
            now=datetime(2026, 3, 19, 10, 0, 1, tzinfo=timezone.utc),
        )
        session = manager.begin_turn(
            session=session,
            speaker_user_id="user-1",
            now=datetime(2026, 3, 19, 10, 1, tzinfo=timezone.utc),
        )
        self.assertTrue(
            manager.can_accept_audio(session=session, speaker_user_id="user-1")
        )
        self.assertFalse(
            manager.can_accept_audio(
                session=session, speaker_user_id="bot-1", is_bot_audio=True
            )
        )
        speaking_session = manager.begin_speaking(
            session=session, now=datetime(2026, 3, 19, 10, 1, 5, tzinfo=timezone.utc)
        )
        self.assertFalse(
            manager.can_accept_audio(session=speaking_session, speaker_user_id="user-1")
        )

    def test_segmenter_finalizes_on_silence_and_explicit_flush(self) -> None:
        binding = LiveVoiceRoomBinding(guild_id="guild-1", voice_channel_id="voice-1")
        segmenter = LiveVoiceTurnSegmenter(
            silence_window=timedelta(seconds=1),
            max_turn_duration=timedelta(seconds=10),
        )
        started = datetime(2026, 3, 19, 10, 0, tzinfo=timezone.utc)
        segmenter.ingest(user_id="user-1", audio_bytes=b"he", received_at=started)
        segmenter.ingest(
            user_id="user-1",
            audio_bytes=b"llo",
            received_at=started + timedelta(milliseconds=200),
        )

        self.assertFalse(
            segmenter.should_finalize(now=started + timedelta(milliseconds=500))
        )
        self.assertTrue(segmenter.should_finalize(now=started + timedelta(seconds=2)))

        turn = segmenter.finalize(
            binding=binding, turn_index=3, now=started + timedelta(seconds=2)
        )
        self.assertIsNotNone(turn)
        self.assertEqual(turn.audio_bytes, b"hello")
        self.assertEqual(turn.turn_index, 3)
        self.assertEqual(turn.finalization_reason, "silence")
        self.assertFalse(segmenter.has_audio)
        self.assertIsNone(segmenter.speaker_user_id)

        segmenter.ingest(
            user_id="user-1",
            audio_bytes=b"again",
            received_at=started + timedelta(seconds=3),
        )
        flushed = segmenter.force_finalize(
            binding=binding,
            turn_index=4,
            now=started + timedelta(seconds=3, milliseconds=1),
            reason="explicit",
        )
        self.assertIsNotNone(flushed)
        self.assertEqual(flushed.audio_bytes, b"again")
        self.assertEqual(flushed.finalization_reason, "explicit")


if __name__ == "__main__":
    unittest.main()
