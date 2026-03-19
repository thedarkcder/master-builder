from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

from orchestrator.core.discord.live_voice_session import (
    LiveVoiceCallbacks,
    LiveVoiceRoomBinding,
    LiveVoiceRoomSession,
    LiveVoiceSessionManager,
    LiveVoiceSessionState,
    LiveVoiceTransitionDecision,
    LiveVoiceTurn,
    LiveVoiceTurnSegmenter,
)


class LiveVoiceRuntimeError(RuntimeError):
    pass


class LiveVoiceTransport(Protocol):
    def join_voice_channel(self, *, binding: LiveVoiceRoomBinding) -> None: ...

    def leave_voice_channel(self, *, binding: LiveVoiceRoomBinding) -> None: ...

    def play_audio(
        self,
        *,
        binding: LiveVoiceRoomBinding,
        audio_bytes: bytes,
        filename: str,
        content_type: str,
    ) -> None: ...


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class LiveVoiceRuntimeService:
    session_manager: LiveVoiceSessionManager = field(default_factory=LiveVoiceSessionManager)
    callbacks: LiveVoiceCallbacks = field(default_factory=LiveVoiceCallbacks)
    clock: Callable[[], datetime] = _utcnow
    _sessions: dict[str, LiveVoiceRoomSession] = field(default_factory=dict)
    _segmenters: dict[str, LiveVoiceTurnSegmenter] = field(default_factory=dict)

    def register_room(
        self,
        *,
        binding: LiveVoiceRoomBinding,
        bot_user_id: str | None = None,
        human_member_count: int = 0,
        now: datetime | None = None,
    ) -> LiveVoiceRoomSession:
        session = self.session_manager.create_session(
            binding=binding,
            bot_user_id=bot_user_id,
            human_member_count=human_member_count,
            now=now or self.clock(),
        )
        self._sessions[binding.room_key] = session
        return session

    def get_session(self, *, binding: LiveVoiceRoomBinding) -> LiveVoiceRoomSession | None:
        return self._sessions.get(binding.room_key)

    def evaluate_join(self, *, binding: LiveVoiceRoomBinding, human_member_count: int) -> LiveVoiceTransitionDecision:
        session = self._require_session(binding=binding)
        return self.session_manager.decide_join(session=session, human_member_count=human_member_count)

    def evaluate_leave(self, *, binding: LiveVoiceRoomBinding, human_member_count: int) -> LiveVoiceTransitionDecision:
        session = self._require_session(binding=binding)
        return self.session_manager.decide_leave(session=session, human_member_count=human_member_count)

    def join_room(
        self,
        *,
        binding: LiveVoiceRoomBinding,
        human_member_count: int,
        now: datetime | None = None,
    ) -> LiveVoiceRoomSession:
        session = self._require_session(binding=binding)
        previous = session
        session = self.session_manager.begin_joining(session=session, now=now or self.clock())
        self._sessions[binding.room_key] = session
        self.callbacks.emit_session_state_changed(previous=previous, current=session)

        previous = session
        session = self.session_manager.mark_joined(
            session=session,
            human_member_count=human_member_count,
            now=now or self.clock(),
        )
        self._sessions[binding.room_key] = session
        self.callbacks.emit_session_state_changed(previous=previous, current=session)
        return session

    def leave_room(
        self,
        *,
        binding: LiveVoiceRoomBinding,
        now: datetime | None = None,
    ) -> LiveVoiceRoomSession:
        session = self._require_session(binding=binding)
        previous = session
        session = self.session_manager.begin_leaving(session=session, now=now or self.clock())
        self._sessions[binding.room_key] = session
        self.callbacks.emit_session_state_changed(previous=previous, current=session)

        previous = session
        session = self.session_manager.mark_disconnected(session=session, now=now or self.clock())
        self._sessions[binding.room_key] = session
        self._segmenters.pop(binding.room_key, None)
        self.callbacks.emit_session_state_changed(previous=previous, current=session)
        return session

    def mark_bot_speaking(
        self,
        *,
        binding: LiveVoiceRoomBinding,
        speaking: bool,
        now: datetime | None = None,
    ) -> LiveVoiceRoomSession:
        session = self._require_session(binding=binding)
        previous = session
        session = (
            self.session_manager.begin_speaking(session=session, now=now or self.clock())
            if speaking
            else self.session_manager.finish_speaking(session=session, now=now or self.clock())
        )
        self._sessions[binding.room_key] = session
        self.callbacks.emit_session_state_changed(previous=previous, current=session)
        return session

    def ingest_audio(
        self,
        *,
        binding: LiveVoiceRoomBinding,
        user_id: str,
        audio_bytes: bytes,
        received_at: datetime | None = None,
        sample_rate_hz: int = 16_000,
        channels: int = 1,
        is_bot_audio: bool = False,
    ) -> LiveVoiceTurn | None:
        session = self._require_session(binding=binding)
        if not self.session_manager.can_accept_audio(
            session=session,
            speaker_user_id=user_id,
            is_bot_audio=is_bot_audio,
        ):
            return None

        if session.state == LiveVoiceSessionState.LISTENING and binding.room_key not in self._segmenters:
            previous = session
            session = self.session_manager.begin_turn(
                session=session,
                speaker_user_id=user_id,
                now=received_at or self.clock(),
            )
            self._sessions[binding.room_key] = session
            self.callbacks.emit_session_state_changed(previous=previous, current=session)
            self._segmenters[binding.room_key] = LiveVoiceTurnSegmenter()

        segmenter = self._segmenters.get(binding.room_key)
        if segmenter is None:
            return None

        segmenter.ingest(
            user_id=user_id,
            audio_bytes=audio_bytes,
            received_at=received_at or self.clock(),
            sample_rate_hz=sample_rate_hz,
            channels=channels,
        )
        session = self.session_manager.mark_turn_audio(session=session, received_at=received_at or self.clock())
        self._sessions[binding.room_key] = session

        if segmenter.finalization_reason(now=received_at or self.clock()) is not None:
            return self._finalize_segmenter(binding=binding, segmenter=segmenter, now=received_at or self.clock())
        return None

    def poll(self, *, now: datetime | None = None) -> list[LiveVoiceTurn]:
        poll_time = now or self.clock()
        finalized_turns: list[LiveVoiceTurn] = []
        for room_key, segmenter in list(self._segmenters.items()):
            session = self._sessions.get(room_key)
            if session is None:
                continue
            if not segmenter.should_finalize(now=poll_time):
                continue
            turn = self._finalize_segmenter(
                binding=session.binding,
                segmenter=segmenter,
                now=poll_time,
            )
            if turn is not None:
                finalized_turns.append(turn)
        return finalized_turns

    def discard_turn(self, *, binding: LiveVoiceRoomBinding, reason: str) -> None:
        session = self._require_session(binding=binding)
        if session.state == LiveVoiceSessionState.PROCESSING:
            previous = session
            session = self.session_manager.mark_turn_finished(session=session, now=self.clock())
            self._sessions[binding.room_key] = session
            self.callbacks.emit_session_state_changed(previous=previous, current=session)
        self._segmenters.pop(binding.room_key, None)
        self.callbacks.emit_turn_discarded(session=session, reason=reason)

    def _finalize_segmenter(
        self,
        *,
        binding: LiveVoiceRoomBinding,
        segmenter: LiveVoiceTurnSegmenter,
        now: datetime,
    ) -> LiveVoiceTurn | None:
        session = self._require_session(binding=binding)
        turn = segmenter.finalize(
            binding=binding,
            turn_index=session.turn_index + 1,
            now=now,
        )
        if turn is None:
            return None
        previous = session
        session = self.session_manager.mark_turn_finished(session=session, now=now)
        self._sessions[binding.room_key] = session
        self._segmenters.pop(binding.room_key, None)
        self.callbacks.emit_session_state_changed(previous=previous, current=session)
        self.callbacks.emit_turn_finalized(session=session, turn=turn)
        return turn

    def _require_session(self, *, binding: LiveVoiceRoomBinding) -> LiveVoiceRoomSession:
        session = self._sessions.get(binding.room_key)
        if session is None:
            raise LiveVoiceRuntimeError(f"Live voice room '{binding.room_key}' is not registered")
        return session


def build_live_voice_runtime(
    *,
    callbacks: LiveVoiceCallbacks | None = None,
    session_manager: LiveVoiceSessionManager | None = None,
    clock: Callable[[], datetime] | None = None,
) -> LiveVoiceRuntimeService:
    return LiveVoiceRuntimeService(
        session_manager=session_manager or LiveVoiceSessionManager(),
        callbacks=callbacks or LiveVoiceCallbacks(),
        clock=clock or _utcnow,
    )
