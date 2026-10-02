from __future__ import annotations

from orchestrator.api.discord.shared.room_history import DiscordRoomHistoryService


def test_append_room_history_entry_keeps_one_room_across_text_voice_note_and_live_voice() -> (
    None
):
    service = DiscordRoomHistoryService(max_history_entries=3, max_history_context=2)
    config: dict = {}

    config, first = service.append_room_history_entry(
        discord_config=config,
        room_id=None,
        channel_id="text-1",
        speaker_type="user",
        source_mode="text",
        text="What should we build?",
        user_id="user-1",
        issue_key=" mab-174 ",
        status_name=" In Progress ",
        metadata={"message_id": "msg-1"},
    )
    config, second = service.append_room_history_entry(
        discord_config=config,
        room_id=None,
        linked_text_channel_id="text-1",
        voice_channel_id="voice-1",
        speaker_type="user",
        source_mode="voice-note",
        text="I want this in the live room too",
        user_id="user-1",
        metadata={"attachment_id": "att-1"},
    )
    config, third = service.append_room_history_entry(
        discord_config=config,
        room_id=None,
        linked_text_channel_id="text-1",
        voice_channel_id="voice-1",
        speaker_type="persona",
        persona_id="pm",
        source_mode="live voice",
        text="Which workflow should lead?",
        metadata={"turn_id": "turn-1"},
    )

    assert first["room_id"] == "text-1"
    assert second["room_id"] == "text-1"
    assert third["room_id"] == "text-1"
    assert first["issue_key"] == "MAB-174"
    assert first["status"] == "In Progress"
    assert second["source_mode"] == "voice_note"
    assert third["source_mode"] == "live_voice"
    assert third["persona_id"] == "pm"
    assert third["channel_id"] == "text-1"
    assert config["persona_room_history"][-1]["text"] == "Which workflow should lead?"

    recent = service.recent_room_history(discord_config=config, channel_id="text-1")
    assert [entry["source_mode"] for entry in recent] == ["voice_note", "live_voice"]
    inferred = service.recent_room_history(
        discord_config=config, voice_channel_id="voice-1"
    )
    assert [entry["source_mode"] for entry in inferred] == ["voice_note", "live_voice"]


def test_recent_room_history_filters_by_persona_and_source_mode() -> None:
    service = DiscordRoomHistoryService(max_history_entries=10, max_history_context=10)
    config: dict = {}
    config, _ = service.append_room_history_entry(
        discord_config=config,
        channel_id="room-1",
        speaker_type="user",
        source_mode="text",
        text="discover",
        user_id="u1",
    )
    config, _ = service.append_room_history_entry(
        discord_config=config,
        channel_id="room-1",
        speaker_type="persona",
        persona_id="pm",
        source_mode="text",
        text="ask more",
    )
    config, _ = service.append_room_history_entry(
        discord_config=config,
        channel_id="room-1",
        speaker_type="persona",
        persona_id="security",
        source_mode="live_voice",
        text="check permissions",
    )

    assert [
        entry["persona_id"]
        for entry in service.recent_room_history(
            discord_config=config,
            channel_id="room-1",
            speaker_types={"persona"},
            source_modes={"live voice"},
        )
    ] == ["security"]


def test_room_history_bounds_entries_without_touching_input_config() -> None:
    service = DiscordRoomHistoryService(max_history_entries=2, max_history_context=2)
    original: dict = {}

    updated, _ = service.append_room_history_entry(
        discord_config=original,
        channel_id="room-1",
        speaker_type="user",
        source_mode="text",
        text="one",
    )
    updated, _ = service.append_room_history_entry(
        discord_config=updated,
        channel_id="room-1",
        speaker_type="persona",
        source_mode="text",
        text="two",
    )
    updated, _ = service.append_room_history_entry(
        discord_config=updated,
        channel_id="room-1",
        speaker_type="user",
        source_mode="text",
        text="three",
    )

    assert original == {}
    assert [entry["text"] for entry in updated["persona_room_history"]] == [
        "two",
        "three",
    ]
