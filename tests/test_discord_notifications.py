from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from orchestrator.core.config import Settings
from orchestrator.core.discord.notifications import send_tenant_discord_message
from orchestrator.storage.models import Project, Tenant


def _tenant(*, notify_events: list[str], channel_id: str = "discord-channel-1") -> Tenant:
    now = datetime.now(timezone.utc)
    return Tenant(
        tenant_id="tenant-discord-test",
        name="Tenant Discord Test",
        is_enabled=True,
        jira_config={},
        github_config={},
        repos_config={},
        policy_config={},
        discord_config={"channel_id": channel_id, "notify_events": notify_events},
        created_at=now,
        updated_at=now,
    )


def _project(*, notify_events: list[str], channel_id: str | None = None) -> Project:
    now = datetime.now(timezone.utc)
    return Project(
        project_id="project-discord-test",
        tenant_id="tenant-discord-test",
        name="Project Discord Test",
        github_repository="https://github.com/example/repo",
        jira_project_key="MAB",
        policy_overrides={},
        environment={},
        secret_refs={},
        discord_config={**({"channel_id": channel_id} if channel_id is not None else {}), "notify_events": notify_events},
        is_archived=False,
        created_at=now,
        updated_at=now,
    )


class DiscordNotificationTests(unittest.TestCase):
    def test_event_disabled_skips_send(self) -> None:
        result = send_tenant_discord_message(
            session=None,  # type: ignore[arg-type]
            tenant=_tenant(notify_events=[]),
            project=_project(notify_events=["lock_acquired"]),
            message="hello",
            settings=Settings(
                discord_bot_token_secret_ref="DISCORD_BOT_TOKEN",
                secrets_encryption_key="test-key",
            ),
            event="review_signal",
        )
        self.assertFalse(result.sent)
        self.assertEqual(result.reason, "event_disabled:review_signal")

    def test_event_enabled_sends_message(self) -> None:
        fake_client = Mock()
        fake_client.post_message.return_value = {"id": "msg-123"}
        with (
            patch("orchestrator.core.discord.notifications.resolve_platform_secret_ref", return_value="bot-token"),
            patch("orchestrator.core.discord.notifications.DiscordApiClient", return_value=fake_client),
        ):
            result = send_tenant_discord_message(
                session=None,  # type: ignore[arg-type]
                tenant=_tenant(notify_events=[]),
                project=_project(notify_events=["review_signal"]),
                message="hello",
                settings=Settings(
                    discord_bot_token_secret_ref="DISCORD_BOT_TOKEN",
                    secrets_encryption_key="test-key",
                ),
                event="review_signal",
            )
        self.assertTrue(result.sent)
        self.assertEqual(result.reason, "sent")
        fake_client.post_message.assert_called_once_with(channel_id="discord-channel-1", content="hello")

    def test_project_channel_is_used_when_configured(self) -> None:
        fake_client = Mock()
        fake_client.post_message.return_value = {"id": "msg-123"}
        with (
            patch("orchestrator.core.discord.notifications.resolve_platform_secret_ref", return_value="bot-token"),
            patch("orchestrator.core.discord.notifications.DiscordApiClient", return_value=fake_client),
        ):
            result = send_tenant_discord_message(
                session=None,  # type: ignore[arg-type]
                tenant=_tenant(notify_events=["review_signal"], channel_id="tenant-channel"),
                project=_project(notify_events=["review_signal"], channel_id="project-channel"),
                message="hello",
                settings=Settings(
                    discord_bot_token_secret_ref="DISCORD_BOT_TOKEN",
                    secrets_encryption_key="test-key",
                ),
                event="review_signal",
            )
        self.assertTrue(result.sent)
        fake_client.post_message.assert_called_once_with(channel_id="project-channel", content="hello")

    def test_tenant_channel_is_used_when_project_channel_missing(self) -> None:
        fake_client = Mock()
        fake_client.post_message.return_value = {"id": "msg-123"}
        with (
            patch("orchestrator.core.discord.notifications.resolve_platform_secret_ref", return_value="bot-token"),
            patch("orchestrator.core.discord.notifications.DiscordApiClient", return_value=fake_client),
        ):
            result = send_tenant_discord_message(
                session=None,  # type: ignore[arg-type]
                tenant=_tenant(notify_events=["review_signal"], channel_id="tenant-channel"),
                project=_project(notify_events=["review_signal"]),
                message="hello",
                settings=Settings(
                    discord_bot_token_secret_ref="DISCORD_BOT_TOKEN",
                    secrets_encryption_key="test-key",
                ),
                event="review_signal",
            )
        self.assertTrue(result.sent)
        fake_client.post_message.assert_called_once_with(channel_id="tenant-channel", content="hello")

    def test_event_enabled_can_open_thread(self) -> None:
        fake_client = Mock()
        fake_client.post_message.return_value = {"id": "msg-123"}
        fake_client.create_thread_from_message.return_value = "thread-456"
        with (
            patch("orchestrator.core.discord.notifications.resolve_platform_secret_ref", return_value="bot-token"),
            patch("orchestrator.core.discord.notifications.DiscordApiClient", return_value=fake_client),
        ):
            result = send_tenant_discord_message(
                session=None,  # type: ignore[arg-type]
                tenant=_tenant(notify_events=[]),
                project=_project(notify_events=["decision_gate_required"]),
                message="decision gate required",
                settings=Settings(
                    discord_bot_token_secret_ref="DISCORD_BOT_TOKEN",
                    secrets_encryption_key="test-key",
                ),
                event="decision_gate_required",
                open_thread=True,
                thread_name="TP-302-decision-gate",
                thread_intro="Reply here",
            )
        self.assertTrue(result.sent)
        fake_client.create_thread_from_message.assert_called_once()
        fake_client.post_message.assert_any_call(channel_id="discord-channel-1", content="decision gate required")
        fake_client.post_message.assert_any_call(channel_id="thread-456", content="Reply here", components=None)

    def test_event_enabled_can_open_thread_with_intro_components(self) -> None:
        fake_client = Mock()
        fake_client.post_message.return_value = {"id": "msg-123"}
        fake_client.create_thread_from_message.return_value = "thread-456"
        with (
            patch("orchestrator.core.discord.notifications.resolve_platform_secret_ref", return_value="bot-token"),
            patch("orchestrator.core.discord.notifications.DiscordApiClient", return_value=fake_client),
        ):
            result = send_tenant_discord_message(
                session=None,  # type: ignore[arg-type]
                tenant=_tenant(notify_events=[]),
                project=_project(notify_events=["decision_gate_required"]),
                message="decision gate required",
                settings=Settings(
                    discord_bot_token_secret_ref="DISCORD_BOT_TOKEN",
                    secrets_encryption_key="test-key",
                ),
                event="decision_gate_required",
                open_thread=True,
                thread_name="TP-302-decision-gate",
                thread_intro="Reply here",
                thread_intro_components=[{"type": 1, "components": [{"type": 2, "custom_id": "ask.reply.open"}]}],
            )
        self.assertTrue(result.sent)
        fake_client.post_message.assert_any_call(
            channel_id="thread-456",
            content="Reply here",
            components=[{"type": 1, "components": [{"type": 2, "custom_id": "ask.reply.open"}]}],
        )


if __name__ == "__main__":
    unittest.main()
