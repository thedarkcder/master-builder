from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


ROOM_HISTORY_CONFIG_KEY = "persona_room_history"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_text(value: object) -> str:
    return " ".join(str(value or "").strip().split())


def _normalize_optional_text(value: object) -> str | None:
    normalized = _normalize_text(value)
    return normalized or None


def _normalize_label(value: object) -> str | None:
    normalized = _normalize_optional_text(value)
    if normalized is None:
        return None
    return normalized.replace(" ", "_").replace("-", "_").lower()


def _normalize_issue_key(value: object) -> str | None:
    normalized = _normalize_optional_text(value)
    if normalized is None:
        return None
    return normalized.upper()


def _normalize_timestamp(value: object) -> str:
    if isinstance(value, datetime):
        normalized = value
        if normalized.tzinfo is None:
            normalized = normalized.replace(tzinfo=timezone.utc)
        return normalized.astimezone(timezone.utc).isoformat()
    normalized = _normalize_optional_text(value)
    return normalized or _utc_now_iso()


def _normalize_metadata(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _normalize_string_set(values: set[str] | None) -> set[str]:
    if not values:
        return set()
    return {
        normalized
        for normalized in (_normalize_label(value) for value in values)
        if normalized
    }


@dataclass(frozen=True)
class RoomHistoryEntry:
    room_id: str
    speaker_type: str
    source_mode: str
    text: str
    created_at: str
    user_id: str | None = None
    persona_id: str | None = None
    channel_id: str | None = None
    thread_channel_id: str | None = None
    voice_channel_id: str | None = None
    linked_text_channel_id: str | None = None
    issue_key: str | None = None
    status: str | None = None
    metadata: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "room_id": self.room_id,
            "speaker_type": self.speaker_type,
            "source_mode": self.source_mode,
            "text": self.text,
            "created_at": self.created_at,
            "user_id": self.user_id,
            "persona_id": self.persona_id,
            "channel_id": self.channel_id,
            "thread_channel_id": self.thread_channel_id,
            "voice_channel_id": self.voice_channel_id,
            "linked_text_channel_id": self.linked_text_channel_id,
            "issue_key": self.issue_key,
            "status": self.status,
            "metadata": dict(self.metadata or {}),
        }


class DiscordRoomHistoryService:
    def __init__(
        self,
        *,
        max_history_entries: int = 120,
        max_history_context: int = 12,
    ) -> None:
        self._max_history_entries = max(1, int(max_history_entries))
        self._max_history_context = max(1, int(max_history_context))

    def resolve_room_id(
        self,
        *,
        room_id: str | None = None,
        channel_id: str | None = None,
        thread_channel_id: str | None = None,
        voice_channel_id: str | None = None,
        linked_text_channel_id: str | None = None,
    ) -> str | None:
        for candidate in (
            room_id,
            linked_text_channel_id,
            thread_channel_id,
            channel_id,
            voice_channel_id,
        ):
            normalized = _normalize_optional_text(candidate)
            if normalized:
                return normalized
        return None

    def build_room_history_entry(
        self,
        *,
        speaker_type: str,
        source_mode: str,
        text: str,
        room_id: str | None = None,
        channel_id: str | None = None,
        thread_channel_id: str | None = None,
        voice_channel_id: str | None = None,
        linked_text_channel_id: str | None = None,
        user_id: str | None = None,
        persona_id: str | None = None,
        issue_key: str | None = None,
        status_name: str | None = None,
        metadata: dict[str, Any] | None = None,
        created_at: object | None = None,
    ) -> RoomHistoryEntry | None:
        resolved_room_id = self.resolve_room_id(
            room_id=room_id,
            channel_id=channel_id,
            thread_channel_id=thread_channel_id,
            voice_channel_id=voice_channel_id,
            linked_text_channel_id=linked_text_channel_id,
        )
        normalized_text = _normalize_text(text)
        normalized_speaker_type = _normalize_label(speaker_type)
        normalized_source_mode = _normalize_label(source_mode)
        if (
            not resolved_room_id
            or not normalized_text
            or not normalized_speaker_type
            or not normalized_source_mode
        ):
            return None

        normalized_channel_id = _normalize_optional_text(channel_id)
        if not normalized_channel_id and normalized_source_mode == "live_voice":
            normalized_channel_id = _normalize_optional_text(linked_text_channel_id)

        return RoomHistoryEntry(
            room_id=resolved_room_id,
            speaker_type=normalized_speaker_type,
            source_mode=normalized_source_mode,
            text=normalized_text,
            created_at=_normalize_timestamp(created_at),
            user_id=_normalize_optional_text(user_id),
            persona_id=_normalize_optional_text(persona_id),
            channel_id=normalized_channel_id,
            thread_channel_id=_normalize_optional_text(thread_channel_id),
            voice_channel_id=_normalize_optional_text(voice_channel_id),
            linked_text_channel_id=_normalize_optional_text(linked_text_channel_id),
            issue_key=_normalize_issue_key(issue_key),
            status=_normalize_optional_text(status_name),
            metadata=_normalize_metadata(metadata),
        )

    def room_history_entries(
        self, *, discord_config: dict | None
    ) -> list[dict[str, Any]]:
        config = dict(discord_config or {})
        raw_entries = config.get(ROOM_HISTORY_CONFIG_KEY)
        if not isinstance(raw_entries, list):
            return []

        normalized: list[dict[str, Any]] = []
        for entry in raw_entries:
            if not isinstance(entry, dict):
                continue
            normalized_entry = self._normalize_existing_entry(entry)
            if normalized_entry is not None:
                normalized.append(normalized_entry.as_dict())
        return normalized

    def recent_room_history(
        self,
        *,
        discord_config: dict | None,
        room_id: str | None = None,
        channel_id: str | None = None,
        thread_channel_id: str | None = None,
        voice_channel_id: str | None = None,
        linked_text_channel_id: str | None = None,
        speaker_types: set[str] | None = None,
        source_modes: set[str] | None = None,
        user_id: str | None = None,
        persona_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        all_entries = self.room_history_entries(discord_config=discord_config)
        resolved_room_id = self.resolve_room_id(
            room_id=room_id,
            channel_id=channel_id,
            thread_channel_id=thread_channel_id,
            voice_channel_id=voice_channel_id,
            linked_text_channel_id=linked_text_channel_id,
        )
        if not resolved_room_id:
            resolved_room_id = self._infer_room_id_from_entries(
                entries=all_entries,
                channel_id=channel_id,
                thread_channel_id=thread_channel_id,
                voice_channel_id=voice_channel_id,
                linked_text_channel_id=linked_text_channel_id,
            )
            if not resolved_room_id:
                return []

        entries = [
            entry for entry in all_entries if entry.get("room_id") == resolved_room_id
        ]
        if not entries:
            inferred_room_id = self._infer_room_id_from_entries(
                entries=all_entries,
                channel_id=channel_id,
                thread_channel_id=thread_channel_id,
                voice_channel_id=voice_channel_id,
                linked_text_channel_id=linked_text_channel_id,
            )
            if inferred_room_id and inferred_room_id != resolved_room_id:
                resolved_room_id = inferred_room_id
                entries = [
                    entry
                    for entry in all_entries
                    if entry.get("room_id") == resolved_room_id
                ]
        if speaker_types:
            allowed_speaker_types = _normalize_string_set(speaker_types)
            entries = [
                entry
                for entry in entries
                if entry.get("speaker_type") in allowed_speaker_types
            ]
        if source_modes:
            allowed_source_modes = _normalize_string_set(source_modes)
            entries = [
                entry
                for entry in entries
                if entry.get("source_mode") in allowed_source_modes
            ]
        normalized_user_id = _normalize_optional_text(user_id)
        if normalized_user_id is not None:
            entries = [
                entry for entry in entries if entry.get("user_id") == normalized_user_id
            ]
        normalized_persona_id = _normalize_optional_text(persona_id)
        if normalized_persona_id is not None:
            entries = [
                entry
                for entry in entries
                if entry.get("persona_id") == normalized_persona_id
            ]

        history_limit = max(
            1,
            limit
            if isinstance(limit, int) and limit > 0
            else self._max_history_context,
        )
        return entries[-history_limit:]

    def append_room_history_entry(
        self,
        *,
        discord_config: dict | None,
        speaker_type: str,
        source_mode: str,
        text: str,
        room_id: str | None = None,
        channel_id: str | None = None,
        thread_channel_id: str | None = None,
        voice_channel_id: str | None = None,
        linked_text_channel_id: str | None = None,
        user_id: str | None = None,
        persona_id: str | None = None,
        issue_key: str | None = None,
        status_name: str | None = None,
        metadata: dict[str, Any] | None = None,
        created_at: object | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        config = dict(discord_config or {})
        normalized_entry = self.build_room_history_entry(
            speaker_type=speaker_type,
            source_mode=source_mode,
            text=text,
            room_id=room_id,
            channel_id=channel_id,
            thread_channel_id=thread_channel_id,
            voice_channel_id=voice_channel_id,
            linked_text_channel_id=linked_text_channel_id,
            user_id=user_id,
            persona_id=persona_id,
            issue_key=issue_key,
            status_name=status_name,
            metadata=metadata,
            created_at=created_at,
        )
        if normalized_entry is None:
            raise ValueError(
                "Room history entry requires a room id, speaker type, source mode, and text"
            )

        entries = self.room_history_entries(discord_config=config)
        entries.append(normalized_entry.as_dict())
        config[ROOM_HISTORY_CONFIG_KEY] = entries[-self._max_history_entries :]
        return config, normalized_entry.as_dict()

    def _normalize_existing_entry(
        self, entry: dict[str, Any]
    ) -> RoomHistoryEntry | None:
        return self.build_room_history_entry(
            room_id=entry.get("room_id"),
            channel_id=entry.get("channel_id"),
            thread_channel_id=entry.get("thread_channel_id"),
            voice_channel_id=entry.get("voice_channel_id"),
            linked_text_channel_id=entry.get("linked_text_channel_id"),
            speaker_type=entry.get("speaker_type") or entry.get("speaker") or "",
            source_mode=entry.get("source_mode") or entry.get("mode") or "",
            text=entry.get("text") or entry.get("message") or "",
            user_id=entry.get("user_id"),
            persona_id=entry.get("persona_id"),
            issue_key=entry.get("issue_key"),
            status_name=entry.get("status"),
            metadata=entry.get("metadata")
            if isinstance(entry.get("metadata"), dict)
            else {},
            created_at=entry.get("created_at"),
        )

    def _infer_room_id_from_entries(
        self,
        *,
        entries: list[dict[str, Any]],
        channel_id: str | None,
        thread_channel_id: str | None,
        voice_channel_id: str | None,
        linked_text_channel_id: str | None,
    ) -> str | None:
        lookup_values = {
            normalized
            for normalized in (
                _normalize_optional_text(channel_id),
                _normalize_optional_text(thread_channel_id),
                _normalize_optional_text(voice_channel_id),
                _normalize_optional_text(linked_text_channel_id),
            )
            if normalized
        }
        if not lookup_values:
            return None

        for entry in reversed(entries):
            entry_room_id = _normalize_optional_text(entry.get("room_id"))
            if entry_room_id in lookup_values:
                return entry_room_id
            if _normalize_optional_text(entry.get("channel_id")) in lookup_values:
                return entry_room_id
            if (
                _normalize_optional_text(entry.get("thread_channel_id"))
                in lookup_values
            ):
                return entry_room_id
            if _normalize_optional_text(entry.get("voice_channel_id")) in lookup_values:
                return entry_room_id
            if (
                _normalize_optional_text(entry.get("linked_text_channel_id"))
                in lookup_values
            ):
                return entry_room_id
        return None

    def tenant_room_history(self, *, tenant) -> list[dict[str, Any]]:
        return self.room_history_entries(
            discord_config=getattr(tenant, "discord_config", None)
        )

    def project_room_history(self, *, project) -> list[dict[str, Any]]:
        return self.room_history_entries(
            discord_config=getattr(project, "discord_config", None)
        )
