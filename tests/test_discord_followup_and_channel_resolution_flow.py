import asyncio
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select

from orchestrator.api.discord.interactions.followup import (
    _build_command_followup_message,
    _run_discord_command_followup,
    _send_discord_thread_followup,
)
from orchestrator.api.discord.interactions.parser import (
    _find_tenant_for_discord_channel,
)
from orchestrator.api.schemas import DiscordCommandResponse
from orchestrator.core.config import get_settings
from orchestrator.storage.models import Project, Tenant
from orchestrator.tools.discord_api import DiscordApiError
from tests.test_support.jira_webhook_api_harness import JiraWebhookTestsHarness


pytestmark = pytest.mark.contract


class DiscordFollowupAndChannelResolutionFlowTests(JiraWebhookTestsHarness):
    def test_discord_followup_formats_issue_and_pr_references_as_hyperlinks(
        self,
    ) -> None:
        with self.session_factory() as session:
            tenant = session.execute(
                select(Tenant).where(Tenant.tenant_id == "tenant-webhook")
            ).scalar_one()
            message = _build_command_followup_message(
                session=session,
                tenant=tenant,
                user_id="discord-user-1",
                command_response=DiscordCommandResponse(
                    ok=True,
                    command="link",
                    message="Links for TP-999",
                    data={
                        "issue_key": "TP-999",
                        "jira_url": "https://example-2.atlassian.net/browse/TP-999",
                        "pr_url": "https://github.com/example/repo/pull/77",
                    },
                ),
            )

        self.assertIn(
            "[TP-999](https://example-2.atlassian.net/browse/TP-999)", message
        )
        self.assertIn("[Open PR](https://github.com/example/repo/pull/77)", message)

    def test_discord_reply_followup_posts_to_thread_without_webhook_followup(
        self,
    ) -> None:
        with (
            patch(
                "orchestrator.api.discord.interactions.followup.execute_discord_ingress_command",
                return_value=DiscordCommandResponse(
                    ok=True,
                    command="ask",
                    message="Done.",
                    data={"issue_key": "TP-324"},
                ),
            ),
            patch(
                "orchestrator.api.discord.interactions.followup._send_discord_thread_followup"
            ) as thread_send_mock,
            patch(
                "orchestrator.api.discord.interactions.followup._send_discord_interaction_followup"
            ) as interaction_send_mock,
        ):
            asyncio.run(
                _run_discord_command_followup(
                    tenant_id="tenant-webhook",
                    user_id="discord-user-1",
                    channel_id="discord-channel-1",
                    command_text="!ask Can you fix it?",
                    application_id="discord-app-1",
                    interaction_token="interaction-token-1",
                    reply_to_message_id="123456789012345678",
                )
            )

        thread_send_mock.assert_called_once()
        interaction_send_mock.assert_not_called()

    def test_discord_interaction_followup_send_failure_is_swallowed(self) -> None:
        with (
            patch(
                "orchestrator.api.discord.interactions.followup.execute_discord_ingress_command",
                return_value=DiscordCommandResponse(
                    ok=True,
                    command="help",
                    message="ok",
                    data=None,
                ),
            ),
            patch(
                "orchestrator.api.discord.interactions.followup._send_discord_interaction_followup",
                side_effect=RuntimeError("token expired"),
            ),
        ):
            asyncio.run(
                _run_discord_command_followup(
                    tenant_id="tenant-webhook",
                    user_id="discord-user-1",
                    channel_id="discord-channel-1",
                    command_text="!help",
                    application_id="discord-app-1",
                    interaction_token="interaction-token-1",
                )
            )

    def test_discord_followup_executes_with_discord_ingress_contract(self) -> None:
        from unittest.mock import create_autospec

        from orchestrator.api.commands.entrypoint import (
            execute_tenant_discord_ingress_command,
        )

        command_executor = create_autospec(
            execute_tenant_discord_ingress_command,
            return_value=DiscordCommandResponse(
                ok=True, command="help", message="ok", data=None
            ),
        )
        with (
            patch(
                "orchestrator.api.discord.interactions.followup.execute_discord_ingress_command",
                command_executor,
            ) as command_mock,
            patch(
                "orchestrator.api.discord.interactions.followup._send_discord_interaction_followup"
            ),
        ):
            asyncio.run(
                _run_discord_command_followup(
                    tenant_id="tenant-webhook",
                    user_id="discord-user-1",
                    channel_id="discord-channel-1",
                    command_text="!help",
                    application_id="discord-app-1",
                    interaction_token="interaction-token-1",
                )
            )

        self.assertEqual(command_mock.call_count, 1)
        self.assertEqual(command_mock.call_args.kwargs["ingress_source"], "discord")

    def test_discord_ask_followup_creates_new_thread_for_initial_response(self) -> None:
        with (
            patch(
                "orchestrator.api.discord.interactions.followup.execute_discord_ingress_command",
                return_value=DiscordCommandResponse(
                    ok=True,
                    command="ask",
                    message="Done.",
                    data={"issue_key": "TP-324"},
                ),
            ),
            patch(
                "orchestrator.api.discord.interactions.followup._send_discord_ask_response_with_thread"
            ) as ask_thread_send_mock,
            patch(
                "orchestrator.api.discord.interactions.followup._send_discord_thread_followup"
            ) as thread_send_mock,
            patch(
                "orchestrator.api.discord.interactions.followup._send_discord_interaction_followup"
            ) as interaction_send_mock,
        ):
            asyncio.run(
                _run_discord_command_followup(
                    tenant_id="tenant-webhook",
                    user_id="discord-user-1",
                    channel_id="discord-channel-1",
                    command_text="!ask Can you fix it?",
                    application_id="discord-app-1",
                    interaction_token="interaction-token-1",
                )
            )

        ask_thread_send_mock.assert_called_once()
        thread_send_mock.assert_not_called()
        interaction_send_mock.assert_called_once_with(
            application_id="discord-app-1",
            interaction_token="interaction-token-1",
            content="Posted response in a follow-up thread.",
            ephemeral=False,
            components=None,
            reply_to_message_id=None,
            channel_id="discord-channel-1",
        )

    def test_send_discord_thread_followup_falls_back_to_current_channel_when_thread_lookup_fails(
        self,
    ) -> None:
        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-webhook")
            self.assertIsNotNone(tenant)

            fake_client = MagicMock()
            fake_client.ensure_thread_for_message.side_effect = DiscordApiError(
                "Cannot create nested thread"
            )
            with (
                patch(
                    "orchestrator.api.discord.interactions.followup.resolve_platform_secret_ref",
                    return_value="bot-token",
                ),
                patch(
                    "orchestrator.api.discord.interactions.followup_transport.DiscordApiClient",
                    return_value=fake_client,
                ),
            ):
                _send_discord_thread_followup(
                    session=session,
                    settings=get_settings(),
                    tenant=tenant,
                    channel_id="discord-thread-1",
                    reply_to_message_id="123456789012345678",
                    content="reply content",
                )

            fake_client.post_message.assert_called_once_with(
                channel_id="discord-thread-1",
                content="reply content",
                components=None,
            )

    def test_send_discord_thread_followup_posts_directly_for_known_thread_channel(
        self,
    ) -> None:
        with self.session_factory() as session:
            project = session.get(Project, "tenant-webhook-default")
            self.assertIsNotNone(project)
            discord_config = dict(project.discord_config or {})
            discord_config["ask_thread_channel_ids"] = ["discord-thread-1"]
            project.discord_config = discord_config
            session.commit()

            fake_client = MagicMock()
            tenant = session.get(Tenant, "tenant-webhook")
            self.assertIsNotNone(tenant)
            with (
                patch(
                    "orchestrator.api.discord.interactions.followup.resolve_platform_secret_ref",
                    return_value="bot-token",
                ),
                patch(
                    "orchestrator.api.discord.interactions.followup_transport.DiscordApiClient",
                    return_value=fake_client,
                ),
            ):
                _send_discord_thread_followup(
                    session=session,
                    settings=get_settings(),
                    tenant=tenant,
                    channel_id="discord-thread-1",
                    reply_to_message_id="123456789012345678",
                    content="reply content",
                )

            fake_client.ensure_thread_for_message.assert_not_called()
            fake_client.post_message.assert_called_once_with(
                channel_id="discord-thread-1",
                content="reply content",
                components=None,
            )

    def test_discord_issues_followup_creates_seed_thread_for_clarifications(
        self,
    ) -> None:
        with (
            patch(
                "orchestrator.api.discord.interactions.followup.execute_discord_ingress_command",
                return_value=DiscordCommandResponse(
                    ok=True,
                    command="issues",
                    message="Issue upsert complete. I still need more detail.",
                    data={
                        "requires_input": True,
                        "followup_request_id": "req-123",
                        "questions": ["What rollout plan should we use?"],
                    },
                ),
            ),
            patch(
                "orchestrator.api.discord.interactions.followup._send_discord_seed_followup_with_thread"
            ) as seed_thread_send_mock,
            patch(
                "orchestrator.api.discord.interactions.followup._send_discord_thread_followup"
            ) as thread_send_mock,
            patch(
                "orchestrator.api.discord.interactions.followup._send_discord_interaction_followup"
            ) as interaction_send_mock,
        ):
            asyncio.run(
                _run_discord_command_followup(
                    tenant_id="tenant-webhook",
                    user_id="discord-user-1",
                    channel_id="discord-channel-1",
                    command_text="!issues seed Build API and worker stories",
                    application_id="discord-app-1",
                    interaction_token="interaction-token-1",
                )
            )

        seed_thread_send_mock.assert_called_once()
        thread_send_mock.assert_not_called()
        interaction_send_mock.assert_called_once_with(
            application_id="discord-app-1",
            interaction_token="interaction-token-1",
            content="Posted response in a follow-up thread.",
            ephemeral=False,
            components=None,
            reply_to_message_id=None,
            channel_id="discord-channel-1",
        )

    def test_find_tenant_for_discord_channel_matches_registered_thread_channel(
        self,
    ) -> None:
        with self.session_factory() as session:
            project = session.get(Project, "tenant-webhook-default")
            self.assertIsNotNone(project)
            discord_config = dict(project.discord_config or {})
            discord_config["ask_thread_channel_ids"] = ["discord-thread-123"]
            project.discord_config = discord_config
            session.commit()

            matched = _find_tenant_for_discord_channel(
                session=session, channel_id="discord-thread-123"
            )
            self.assertIsNotNone(matched)
            self.assertEqual(matched.tenant_id, "tenant-webhook")

    def test_find_tenant_for_discord_channel_matches_seed_followup_thread_channel(
        self,
    ) -> None:
        with self.session_factory() as session:
            project = session.get(Project, "tenant-webhook-default")
            self.assertIsNotNone(project)
            discord_config = dict(project.discord_config or {})
            discord_config["seed_followup_thread_channel_ids"] = [
                "discord-thread-seed-1"
            ]
            project.discord_config = discord_config
            session.commit()

            matched = _find_tenant_for_discord_channel(
                session=session, channel_id="discord-thread-seed-1"
            )
            self.assertIsNotNone(matched)
            self.assertEqual(matched.tenant_id, "tenant-webhook")

    def test_find_tenant_for_discord_channel_matches_project_discord_channel(
        self,
    ) -> None:
        with self.session_factory() as session:
            project = session.execute(
                select(Project).where(
                    Project.tenant_id == "tenant-webhook",
                    Project.is_archived.is_(False),
                )
            ).scalar_one_or_none()
            self.assertIsNotNone(project)
            project.discord_config = {
                "channel_id": "discord-project-channel-1",
                "ask_thread_channel_ids": ["discord-project-thread-1"],
            }
            session.commit()

            matched = _find_tenant_for_discord_channel(
                session=session, channel_id="discord-project-thread-1"
            )
            self.assertIsNotNone(matched)
            self.assertEqual(matched.tenant_id, "tenant-webhook")

    def test_find_tenant_for_discord_channel_returns_none_when_channel_is_shared(
        self,
    ) -> None:
        self._create_tenant("tenant-webhook-2")
        with self.session_factory() as session:
            project_one = session.execute(
                select(Project).where(
                    Project.tenant_id == "tenant-webhook",
                    Project.is_archived.is_(False),
                )
            ).scalar_one_or_none()
            project_two = session.execute(
                select(Project).where(
                    Project.tenant_id == "tenant-webhook-2",
                    Project.is_archived.is_(False),
                )
            ).scalar_one_or_none()
            assert project_one is not None
            assert project_two is not None
            project_one.discord_config = {"channel_id": "discord-shared-channel"}
            project_two.discord_config = {"channel_id": "discord-shared-channel"}
            session.commit()

            matched = _find_tenant_for_discord_channel(
                session=session, channel_id="discord-shared-channel"
            )
            self.assertIsNone(matched)

    def test_find_tenant_for_discord_channel_refreshes_after_project_channel_change(
        self,
    ) -> None:
        with self.session_factory() as session:
            project = session.execute(
                select(Project).where(
                    Project.tenant_id == "tenant-webhook",
                    Project.is_archived.is_(False),
                )
            ).scalar_one_or_none()
            assert project is not None
            project.discord_config = {"channel_id": "discord-dynamic-1"}
            session.commit()

            first_match = _find_tenant_for_discord_channel(
                session=session, channel_id="discord-dynamic-1"
            )
            self.assertIsNotNone(first_match)
            assert first_match is not None
            self.assertEqual(first_match.tenant_id, "tenant-webhook")

            project.discord_config = {"channel_id": "discord-dynamic-2"}
            session.commit()

            stale_match = _find_tenant_for_discord_channel(
                session=session, channel_id="discord-dynamic-1"
            )
            self.assertIsNone(stale_match)

            refreshed_match = _find_tenant_for_discord_channel(
                session=session, channel_id="discord-dynamic-2"
            )
            self.assertIsNotNone(refreshed_match)
            assert refreshed_match is not None
            self.assertEqual(refreshed_match.tenant_id, "tenant-webhook")
