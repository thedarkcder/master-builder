from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, patch

from orchestrator.api.admin.tenant_project_helpers import ensure_default_project_for_tenant, resolve_project_discord_channel_binding
from orchestrator.storage.models import Project
from orchestrator.tools.discord_api import DiscordApiError


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        discord_channel_category_id="text-category-1",
        secrets_encryption_key="test-key",
    )


def _tenant() -> SimpleNamespace:
    return SimpleNamespace(tenant_id="tenant-1", discord_config={"guild_id": "guild-1"})


def _project() -> SimpleNamespace:
    return SimpleNamespace(project_id="project-1", name="Alpha Project", jira_project_key="TP")


def test_ensure_default_project_uses_opaque_project_id() -> None:
    session = Mock()
    session.get.return_value = None
    session.execute.return_value.scalar_one_or_none.return_value = None
    added: list[Project] = []
    session.add.side_effect = added.append
    tenant = SimpleNamespace(
        tenant_id="tenant-1",
        repos_config={"github_repository": "https://github.com/example/align"},
        jira_config={"project_keys": ["AP"]},
    )

    ensure_default_project_for_tenant(
        session,
        tenant=tenant,
        default_project_name_from_repo_fn=lambda **_: "align",
    )

    assert len(added) == 1
    assert added[0].project_id.startswith("proj_")
    assert "tenant-1" not in added[0].project_id
    assert added[0].name == "align"


def test_resolve_project_discord_channel_binding_preserves_existing_text_and_voice_channels() -> None:
    fake_client = Mock()
    fake_client.get_channel.side_effect = [
        {"id": "text-1"},
        {"id": "voice-1"},
    ]

    with (
        patch("orchestrator.api.admin.tenant_project_helpers.resolve_platform_secret_ref", return_value="discord-bot-token"),
        patch("orchestrator.api.admin.tenant_project_helpers.DiscordApiClient", return_value=fake_client),
    ):
        result = resolve_project_discord_channel_binding(
            session=Mock(),
            settings=_settings(),
            tenant=_tenant(),
            project=_project(),
            discord_config={
                "channel_id": "text-1",
                "live_voice_room_links": {"voice-1": "text-1"},
                "voice_room_channel_ids": ["voice-1"],
            },
            resolve_project_discord_channel_name_fn=lambda **_: "alpha-project",
        )

    assert result["channel_id"] == "text-1"
    assert result["live_voice_room_links"] == {"voice-1": "text-1"}
    assert result["voice_room_channel_ids"] == ["voice-1"]
    assert result["voice_room_channel_id"] == "voice-1"
    fake_client.ensure_text_channel.assert_not_called()
    fake_client.ensure_voice_channel.assert_not_called()


def test_resolve_project_discord_channel_binding_remaps_existing_voice_channel_to_recreated_text_channel() -> None:
    fake_client = Mock()
    fake_client.get_channel.side_effect = [
        DiscordApiError("missing text"),
        {"id": "voice-1"},
        DiscordApiError("missing old linked text"),
    ]
    fake_client.ensure_text_channel.return_value = SimpleNamespace(channel_id="text-2")

    with (
        patch("orchestrator.api.admin.tenant_project_helpers.resolve_platform_secret_ref", return_value="discord-bot-token"),
        patch("orchestrator.api.admin.tenant_project_helpers.DiscordApiClient", return_value=fake_client),
    ):
        result = resolve_project_discord_channel_binding(
            session=Mock(),
            settings=_settings(),
            tenant=_tenant(),
            project=_project(),
            discord_config={
                "channel_id": "text-1",
                "live_voice_room_links": {"voice-1": "text-1"},
            },
            resolve_project_discord_channel_name_fn=lambda **_: "alpha-project",
        )

    assert result["channel_id"] == "text-2"
    assert result["live_voice_room_links"] == {"voice-1": "text-2"}
    fake_client.ensure_text_channel.assert_called_once_with(
        guild_id="guild-1",
        name="alpha-project",
        parent_id="text-category-1",
    )
    fake_client.ensure_voice_channel.assert_not_called()


def test_resolve_project_discord_channel_binding_recreates_text_and_voice_channels_when_missing() -> None:
    fake_client = Mock()
    fake_client.get_channel.side_effect = [DiscordApiError("missing text")]
    fake_client.ensure_text_channel.return_value = SimpleNamespace(channel_id="text-2")
    fake_client.ensure_voice_channel.return_value = SimpleNamespace(channel_id="voice-2")
    fake_client.list_channel_categories.return_value = [
        SimpleNamespace(channel_id="voice-category-1", name="Voice Rooms"),
        SimpleNamespace(channel_id="text-category-1", name="Projects"),
    ]
    fake_client.list_voice_channels.return_value = []

    with (
        patch("orchestrator.api.admin.tenant_project_helpers.resolve_platform_secret_ref", return_value="discord-bot-token"),
        patch("orchestrator.api.admin.tenant_project_helpers.DiscordApiClient", return_value=fake_client),
    ):
        result = resolve_project_discord_channel_binding(
            session=Mock(),
            settings=_settings(),
            tenant=_tenant(),
            project=_project(),
            discord_config={"channel_id": "text-1"},
            resolve_project_discord_channel_name_fn=lambda **_: "alpha-project",
        )

    assert result["channel_id"] == "text-2"
    assert result["live_voice_enabled"] is True
    assert result["live_voice_room_links"] == {"voice-2": "text-2"}
    assert result["voice_room_channel_ids"] == ["voice-2"]
    assert result["voice_room_channel_id"] == "voice-2"
    fake_client.ensure_text_channel.assert_called_once_with(
        guild_id="guild-1",
        name="alpha-project",
        parent_id="text-category-1",
    )
    fake_client.ensure_voice_channel.assert_called_once_with(
        guild_id="guild-1",
        name="alpha-project-voice",
        parent_id="voice-category-1",
    )


def test_resolve_project_discord_channel_binding_falls_back_to_text_category_when_no_voice_category_is_discoverable() -> None:
    fake_client = Mock()
    fake_client.ensure_text_channel.return_value = SimpleNamespace(channel_id="text-2")
    fake_client.ensure_voice_channel.return_value = SimpleNamespace(channel_id="voice-2")
    fake_client.list_channel_categories.return_value = [
        SimpleNamespace(channel_id="text-category-1", name="Projects"),
    ]
    fake_client.list_voice_channels.return_value = []

    with (
        patch("orchestrator.api.admin.tenant_project_helpers.resolve_platform_secret_ref", return_value="discord-bot-token"),
        patch("orchestrator.api.admin.tenant_project_helpers.DiscordApiClient", return_value=fake_client),
    ):
        resolve_project_discord_channel_binding(
            session=Mock(),
            settings=_settings(),
            tenant=_tenant(),
            project=_project(),
            discord_config={},
            resolve_project_discord_channel_name_fn=lambda **_: "alpha-project",
        )

    fake_client.ensure_text_channel.assert_called_once_with(
        guild_id="guild-1",
        name="alpha-project",
        parent_id="text-category-1",
    )
    fake_client.ensure_voice_channel.assert_called_once_with(
        guild_id="guild-1",
        name="alpha-project-voice",
        parent_id="text-category-1",
    )
