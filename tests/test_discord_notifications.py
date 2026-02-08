from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from orchestrator.core.config import Settings
from orchestrator.core.discord_notifications import send_tenant_discord_message
from orchestrator.storage.models import Tenant


def _tenant(*, notify_events: list[str]) -> Tenant:
    now = datetime.now(timezone.utc)
    return Tenant(
        tenant_id="tenant-discord-test",
        name="Tenant Discord Test",
        is_enabled=True,
        jira_config={},
        github_config={},
        repos_config={},
        policy_config={},
        discord_config={
            "channel_id": "discord-channel-1",
            "notify_events": notify_events,
        },
        created_at=now,
        updated_at=now,
    )


class DiscordNotificationTests(unittest.TestCase):
    def test_event_disabled_skips_send(self) -> None:
        result = send_tenant_discord_message(
            session=None,  # type: ignore[arg-type]
            tenant=_tenant(notify_events=["lock_acquired"]),
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
        with (
            patch("orchestrator.core.discord_notifications.resolve_secret_ref", return_value="bot-token"),
            patch("orchestrator.core.discord_notifications.DiscordApiClient", return_value=fake_client),
        ):
            result = send_tenant_discord_message(
                session=None,  # type: ignore[arg-type]
                tenant=_tenant(notify_events=["review_signal"]),
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


if __name__ == "__main__":
    unittest.main()
