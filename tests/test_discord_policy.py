from orchestrator.core.discord.policy import (
    can_execute_sensitive_command,
    channel_ids_from_discord_config,
    is_channel_allowed,
    normalize_allowlisted_user_ids,
)


def test_normalize_allowlisted_user_ids_filters_blank_values() -> None:
    values = normalize_allowlisted_user_ids({"allowed_user_ids": ["u1", " ", "", "u2"]})
    assert values == {"u1", "u2"}


def test_channel_ids_from_discord_config_collects_threads() -> None:
    values = channel_ids_from_discord_config(
        {
            "channel_id": "main",
            "ask_thread_channel_ids": ["ask-1"],
            "seed_followup_thread_channel_ids": ["seed-1"],
        }
    )
    assert values == {"main", "ask-1", "seed-1"}


def test_channel_ids_from_discord_config_collects_live_voice_room_ids() -> None:
    values = channel_ids_from_discord_config(
        {
            "channel_id": "main",
            "live_voice_room_links": {
                "voice-1": "text-1",
                " voice-2 ": " text-2 ",
            },
        }
    )
    assert values == {"main", "voice-1", "text-1", "voice-2", "text-2"}


def test_channel_ids_from_discord_config_collects_project_room_channels() -> None:
    values = channel_ids_from_discord_config(
        {
            "pm_room_channel_ids": ["pm-room-1", " "],
            "persona_room_thread_channel_id": "persona-thread-1",
            "room_thread_channel_ids": ["room-thread-1"],
        }
    )
    assert values == {"pm-room-1", "persona-thread-1", "room-thread-1"}


def test_can_execute_sensitive_command_returns_reason_when_not_allowed() -> None:
    allowed, reason = can_execute_sensitive_command(
        command_name="run",
        user_id="u1",
        has_project_mapping=False,
        tenant_allowlist=set(),
        project_allowlist=set(),
    )
    assert not allowed
    assert "project-mapped" in (reason or "")


def test_is_channel_allowed_uses_allowlist_when_present() -> None:
    assert is_channel_allowed(channel_id="c1", allowed_channel_ids={"c1"})
    assert not is_channel_allowed(channel_id="c2", allowed_channel_ids={"c1"})
