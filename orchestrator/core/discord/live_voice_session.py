from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Protocol


class LiveVoiceSessionError(RuntimeError):
    pass


class LiveVoiceSessionState(str, Enum):
    DISCONNECTED = "disconnected"
    JOINING = "joining"
    LISTENING = "listening"
    PROCESSING = "processing"
    SPEAKING = "speaking"
    LEAVING = "leaving"


@dataclass(frozen=True)
class LiveVoiceRoomBinding:
    guild_id: str
    voice_channel_id: str
    text_channel_id: str | None = None

    @property
    def room_key(self) -> str:
        return f"{self.guild_id.strip()}:{self.voice_channel_id.strip()}"


@dataclass(frozen=True)
class LiveVoiceRoomSession:
    binding: LiveVoiceRoomBinding
    state: LiveVoiceSessionState = LiveVoiceSessionState.DISCONNECTED
    human_member_count: int = 0
    bot_user_id: str | None = None
    turn_index: int = 0
    bot_speaking: bool = False
    active_speaker_user_id: str | None = None
    joined_at: datetime | None = None
    last_state_change_at: datetime | None = None
    last_audio_at: datetime | None = None
    current_turn_started_at: datetime | None = None
    current_turn_last_audio_at: datetime | None = None
    last_turn_finished_at: datetime | None = None

    @property
    def room_key(self) -> str:
        return self.binding.room_key


@dataclass(frozen=True)
class LiveVoiceTransitionDecision:
    action: str
    should_transition: bool
    reason: str


@dataclass(frozen=True)
class LiveVoiceTurn:
    binding: LiveVoiceRoomBinding
    turn_index: int
    user_id: str
    audio_bytes: bytes
    started_at: datetime
    ended_at: datetime
    sample_rate_hz: int
    channels: int
    finalization_reason: str

    @property
    def room_key(self) -> str:
        return self.binding.room_key


@dataclass
class LiveVoiceTurnSegmenter:
    silence_window: timedelta = timedelta(seconds=1.25)
    max_turn_duration: timedelta = timedelta(seconds=30)
    _speaker_user_id: str | None = None
    _started_at: datetime | None = None
    _last_audio_at: datetime | None = None
    _chunks: list[bytes] = field(default_factory=list)
    _sample_rate_hz: int = 0
    _channels: int = 0

    def ingest(
        self,
        *,
        user_id: str,
        audio_bytes: bytes,
        received_at: datetime,
        sample_rate_hz: int = 16_000,
        channels: int = 1,
    ) -> None:
        normalized_user_id = self._normalize_identifier(user_id, field_name="user_id")
        if not audio_bytes:
            raise LiveVoiceSessionError("Audio payload cannot be empty")
        self._validate_timestamp(received_at)
        if self._speaker_user_id is None:
            self._speaker_user_id = normalized_user_id
            self._started_at = received_at
            self._sample_rate_hz = int(sample_rate_hz)
            self._channels = int(channels)
        elif normalized_user_id != self._speaker_user_id:
            raise LiveVoiceSessionError("Turn segmenter is already bound to a different speaker")
        elif int(sample_rate_hz) != self._sample_rate_hz or int(channels) != self._channels:
            raise LiveVoiceSessionError("Audio format changed mid-turn")
        self._chunks.append(bytes(audio_bytes))
        self._last_audio_at = received_at

    def should_finalize(self, *, now: datetime) -> bool:
        self._validate_timestamp(now)
        if not self._chunks or self._started_at is None or self._last_audio_at is None:
            return False
        return self.finalization_reason(now=now) is not None

    def finalization_reason(self, *, now: datetime) -> str | None:
        self._validate_timestamp(now)
        if not self._chunks or self._started_at is None or self._last_audio_at is None:
            return None
        if now - self._last_audio_at >= self.silence_window:
            return "silence"
        if now - self._started_at >= self.max_turn_duration:
            return "max_duration"
        return None

    def finalize(
        self,
        *,
        binding: LiveVoiceRoomBinding,
        turn_index: int,
        now: datetime,
        reason: str | None = None,
    ) -> LiveVoiceTurn | None:
        self._validate_timestamp(now)
        if not self._chunks or self._speaker_user_id is None or self._started_at is None or self._last_audio_at is None:
            return None
        final_reason = reason or self.finalization_reason(now=now)
        if final_reason is None:
            return None
        turn = LiveVoiceTurn(
            binding=binding,
            turn_index=int(turn_index),
            user_id=self._speaker_user_id,
            audio_bytes=b"".join(self._chunks),
            started_at=self._started_at,
            ended_at=self._last_audio_at,
            sample_rate_hz=self._sample_rate_hz,
            channels=self._channels,
            finalization_reason=final_reason,
        )
        self.reset()
        return turn

    def force_finalize(
        self,
        *,
        binding: LiveVoiceRoomBinding,
        turn_index: int,
        now: datetime,
        reason: str = "explicit",
    ) -> LiveVoiceTurn | None:
        return self.finalize(binding=binding, turn_index=turn_index, now=now, reason=reason)

    def reset(self) -> None:
        self._speaker_user_id = None
        self._started_at = None
        self._last_audio_at = None
        self._chunks.clear()
        self._sample_rate_hz = 0
        self._channels = 0

    @property
    def has_audio(self) -> bool:
        return bool(self._chunks)

    @property
    def speaker_user_id(self) -> str | None:
        return self._speaker_user_id

    @property
    def started_at(self) -> datetime | None:
        return self._started_at

    @property
    def last_audio_at(self) -> datetime | None:
        return self._last_audio_at

    @staticmethod
    def _normalize_identifier(value: str, *, field_name: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise LiveVoiceSessionError(f"{field_name} cannot be empty")
        return normalized

    @staticmethod
    def _validate_timestamp(value: datetime) -> None:
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise LiveVoiceSessionError("Timestamps must be timezone-aware")


class LiveVoiceRuntimeCallbacks(Protocol):
    def on_session_state_changed(
        self,
        *,
        previous: LiveVoiceRoomSession,
        current: LiveVoiceRoomSession,
    ) -> None: ...

    def on_turn_finalized(
        self,
        *,
        session: LiveVoiceRoomSession,
        turn: LiveVoiceTurn,
    ) -> None: ...

    def on_turn_discarded(
        self,
        *,
        session: LiveVoiceRoomSession,
        reason: str,
    ) -> None: ...


@dataclass(frozen=True)
class LiveVoiceCallbacks:
    on_session_state_changed: Callable[..., None] | None = None
    on_turn_finalized: Callable[..., None] | None = None
    on_turn_discarded: Callable[..., None] | None = None

    def emit_session_state_changed(self, *, previous: LiveVoiceRoomSession, current: LiveVoiceRoomSession) -> None:
        if self.on_session_state_changed is not None:
            self.on_session_state_changed(previous=previous, current=current)

    def emit_turn_finalized(self, *, session: LiveVoiceRoomSession, turn: LiveVoiceTurn) -> None:
        if self.on_turn_finalized is not None:
            self.on_turn_finalized(session=session, turn=turn)

    def emit_turn_discarded(self, *, session: LiveVoiceRoomSession, reason: str) -> None:
        if self.on_turn_discarded is not None:
            self.on_turn_discarded(session=session, reason=reason)


class LiveVoiceSessionManager:
    def create_session(
        self,
        *,
        binding: LiveVoiceRoomBinding,
        bot_user_id: str | None = None,
        human_member_count: int = 0,
        now: datetime | None = None,
    ) -> LiveVoiceRoomSession:
        normalized_now = self._now(now)
        return LiveVoiceRoomSession(
            binding=binding,
            state=LiveVoiceSessionState.DISCONNECTED,
            human_member_count=max(0, int(human_member_count)),
            bot_user_id=str(bot_user_id).strip() if bot_user_id is not None and str(bot_user_id).strip() else None,
            last_state_change_at=normalized_now,
        )

    def decide_join(self, *, session: LiveVoiceRoomSession, human_member_count: int) -> LiveVoiceTransitionDecision:
        if session.state != LiveVoiceSessionState.DISCONNECTED:
            return LiveVoiceTransitionDecision(action="join", should_transition=False, reason="session already active")
        if int(human_member_count) <= 0:
            return LiveVoiceTransitionDecision(action="join", should_transition=False, reason="no human listeners present")
        return LiveVoiceTransitionDecision(action="join", should_transition=True, reason="human listeners joined")

    def decide_leave(self, *, session: LiveVoiceRoomSession, human_member_count: int) -> LiveVoiceTransitionDecision:
        if session.state in {LiveVoiceSessionState.DISCONNECTED, LiveVoiceSessionState.LEAVING}:
            return LiveVoiceTransitionDecision(action="leave", should_transition=False, reason="session not active")
        if session.bot_speaking:
            return LiveVoiceTransitionDecision(action="leave", should_transition=False, reason="bot is speaking")
        if int(human_member_count) > 0:
            return LiveVoiceTransitionDecision(action="leave", should_transition=False, reason="listeners remain in room")
        if session.state == LiveVoiceSessionState.PROCESSING:
            return LiveVoiceTransitionDecision(action="leave", should_transition=False, reason="turn still buffering")
        return LiveVoiceTransitionDecision(action="leave", should_transition=True, reason="room is empty")

    def begin_joining(self, *, session: LiveVoiceRoomSession, now: datetime | None = None) -> LiveVoiceRoomSession:
        return self._with_state(session, state=LiveVoiceSessionState.JOINING, now=now)

    def mark_joined(self, *, session: LiveVoiceRoomSession, human_member_count: int, now: datetime | None = None) -> LiveVoiceRoomSession:
        normalized_now = self._now(now)
        return replace(
            session,
            state=LiveVoiceSessionState.LISTENING,
            human_member_count=max(0, int(human_member_count)),
            joined_at=session.joined_at or normalized_now,
            last_state_change_at=normalized_now,
        )

    def begin_turn(
        self,
        *,
        session: LiveVoiceRoomSession,
        speaker_user_id: str,
        now: datetime | None = None,
    ) -> LiveVoiceRoomSession:
        if session.bot_speaking:
            raise LiveVoiceSessionError("Cannot begin a turn while the bot is speaking")
        if session.state != LiveVoiceSessionState.LISTENING:
            raise LiveVoiceSessionError("Can only begin a turn while listening")
        normalized_now = self._now(now)
        normalized_speaker = self._normalize_identifier(speaker_user_id, field_name="speaker_user_id")
        return replace(
            session,
            state=LiveVoiceSessionState.PROCESSING,
            active_speaker_user_id=normalized_speaker,
            current_turn_started_at=normalized_now,
            current_turn_last_audio_at=normalized_now,
            last_audio_at=normalized_now,
            last_state_change_at=normalized_now,
        )

    def mark_turn_audio(
        self,
        *,
        session: LiveVoiceRoomSession,
        received_at: datetime | None = None,
    ) -> LiveVoiceRoomSession:
        normalized_now = self._now(received_at)
        if session.state != LiveVoiceSessionState.PROCESSING:
            raise LiveVoiceSessionError("Cannot record turn audio unless a turn is active")
        return replace(
            session,
            current_turn_last_audio_at=normalized_now,
            last_audio_at=normalized_now,
            last_state_change_at=normalized_now,
        )

    def mark_turn_finished(self, *, session: LiveVoiceRoomSession, now: datetime | None = None) -> LiveVoiceRoomSession:
        normalized_now = self._now(now)
        if session.state != LiveVoiceSessionState.PROCESSING:
            raise LiveVoiceSessionError("Cannot finish a turn unless a turn is active")
        return replace(
            session,
            state=LiveVoiceSessionState.LISTENING,
            active_speaker_user_id=None,
            current_turn_started_at=None,
            current_turn_last_audio_at=None,
            turn_index=session.turn_index + 1,
            last_turn_finished_at=normalized_now,
            last_state_change_at=normalized_now,
        )

    def begin_speaking(self, *, session: LiveVoiceRoomSession, now: datetime | None = None) -> LiveVoiceRoomSession:
        normalized_now = self._now(now)
        if session.bot_speaking:
            return session
        return replace(
            session,
            state=LiveVoiceSessionState.SPEAKING,
            bot_speaking=True,
            last_state_change_at=normalized_now,
        )

    def finish_speaking(self, *, session: LiveVoiceRoomSession, now: datetime | None = None) -> LiveVoiceRoomSession:
        normalized_now = self._now(now)
        if not session.bot_speaking and session.state != LiveVoiceSessionState.SPEAKING:
            return session
        return replace(
            session,
            state=LiveVoiceSessionState.LISTENING,
            bot_speaking=False,
            last_state_change_at=normalized_now,
        )

    def begin_leaving(self, *, session: LiveVoiceRoomSession, now: datetime | None = None) -> LiveVoiceRoomSession:
        return self._with_state(session, state=LiveVoiceSessionState.LEAVING, now=now)

    def mark_disconnected(self, *, session: LiveVoiceRoomSession, now: datetime | None = None) -> LiveVoiceRoomSession:
        normalized_now = self._now(now)
        return replace(
            session,
            state=LiveVoiceSessionState.DISCONNECTED,
            bot_speaking=False,
            active_speaker_user_id=None,
            current_turn_started_at=None,
            current_turn_last_audio_at=None,
            last_state_change_at=normalized_now,
        )

    def can_accept_audio(
        self,
        *,
        session: LiveVoiceRoomSession,
        speaker_user_id: str,
        is_bot_audio: bool = False,
    ) -> bool:
        normalized_speaker = self._normalize_identifier(speaker_user_id, field_name="speaker_user_id")
        if is_bot_audio or session.bot_speaking:
            return False
        if session.bot_user_id is not None and normalized_speaker == session.bot_user_id:
            return False
        if session.state == LiveVoiceSessionState.SPEAKING:
            return False
        if session.state not in {LiveVoiceSessionState.LISTENING, LiveVoiceSessionState.PROCESSING}:
            return False
        if session.state == LiveVoiceSessionState.PROCESSING and session.active_speaker_user_id not in {None, normalized_speaker}:
            return False
        return True

    def _with_state(
        self,
        session: LiveVoiceRoomSession,
        *,
        state: LiveVoiceSessionState,
        now: datetime | None = None,
    ) -> LiveVoiceRoomSession:
        normalized_now = self._now(now)
        return replace(session, state=state, last_state_change_at=normalized_now)

    @staticmethod
    def _normalize_identifier(value: str, *, field_name: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise LiveVoiceSessionError(f"{field_name} cannot be empty")
        return normalized

    @staticmethod
    def _now(value: datetime | None) -> datetime:
        if value is None:
            return datetime.now(timezone.utc)
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise LiveVoiceSessionError("Timestamps must be timezone-aware")
        return value
