from __future__ import annotations

import json
from dataclasses import asdict
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock

from orchestrator.api.discord.shared.reply_transport import DiscordReplyTransport
from orchestrator.api.webhooks.github_application import GitHubTransportExecutor
from orchestrator.core.communications.contracts import (
    ActorIdentity,
    CommandRequest,
    CommunicationAction,
    CommunicationEvent,
    CommunicationLink,
    DiscordChannelMessageAction,
    DiscordInteractionResponseAction,
    GitHubIssueCommentReactionAction,
    GitHubPullRequestReviewCommentReactionAction,
    HttpJsonResponseAction,
    HttpJsonResponseBytesAction,
    InboundMessage,
    ProjectScope,
)
from orchestrator.api.transport_runtime import HttpTransportExecutor
from orchestrator.core.discord.transport_executor import DiscordTransportExecutor


class CommunicationContractsTests(unittest.TestCase):
    def test_inbound_message_requires_explicit_project_scope(self) -> None:
        scope = ProjectScope(
            tenant_id="tenant-a",
            project_id="tenant-a-default",
            jira_project_key="TP",
            channel_id="discord-channel-1",
        )
        inbound = InboundMessage(
            source="discord",
            source_event_type="application_command",
            tenant_id="tenant-a",
            project_scope=scope,
            actor=ActorIdentity(actor_id="u-1", display_name="user"),
            message_text="!ask what's blocked",
            channel_or_thread_ref="discord-channel-1",
            correlation_id="corr-1",
            idempotency_key="event-1",
            metadata={"raw_command": "!ask what's blocked"},
            received_at=datetime.now(timezone.utc),
        )
        request = CommandRequest(
            command_name="ask",
            arguments=("what's", "blocked"),
            inbound=inbound,
        )
        self.assertEqual(request.inbound.project_scope.jira_project_key, "TP")
        self.assertEqual(request.inbound.source, "discord")

    def test_communication_event_links_and_actions_are_typed(self) -> None:
        event = CommunicationEvent(
            event_type="decision_gate_required",
            tenant_id="tenant-a",
            project_id="tenant-a-default",
            run_id="run-123",
            issue_key="TP-10",
            title="Decision Gate Required",
            summary_markdown="Need GTD clarifications before retry.",
            links=(CommunicationLink(label="Issue", url="https://example.atlassian.net/browse/TP-10"),),
            actions=(CommunicationAction(action_id="reply", label="Reply", kind="button"),),
        )
        self.assertEqual(event.links[0].label, "Issue")
        self.assertEqual(event.actions[0].action_id, "reply")

    def test_contract_snapshot_shape_is_stable(self) -> None:
        scope = ProjectScope(
            tenant_id="tenant-a",
            project_id="tenant-a-default",
            jira_project_key="TP",
            channel_id="discord-channel-1",
        )
        inbound = InboundMessage(
            source="discord",
            source_event_type="application_command",
            tenant_id="tenant-a",
            project_scope=scope,
            actor=ActorIdentity(actor_id="u-1"),
            message_text="!help",
            correlation_id="corr-1",
        )
        payload = asdict(
            CommunicationEvent(
                event_type="run_started",
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                run_id="run-1",
                issue_key="TP-1",
                title="Run started",
                summary_markdown="Run started for TP-1",
                links=(CommunicationLink(label="Issue", url="https://example/browse/TP-1"),),
                actions=(CommunicationAction(action_id="reply", label="Reply", kind="button"),),
                correlation_id=inbound.correlation_id,
            )
        )
        snapshot = json.dumps(payload, sort_keys=True, default=str)
        self.assertIn('"event_type": "run_started"', snapshot)
        self.assertIn('"action_id": "reply"', snapshot)

    def test_http_transport_executor_supports_typed_http_actions(self) -> None:
        executor = HttpTransportExecutor()
        json_response = executor.execute(action=HttpJsonResponseAction(status_code=200, content={"ok": True}))
        self.assertEqual(json_response.status_code, 200)

        raw_response = executor.execute(
            action=HttpJsonResponseBytesAction(status_code=202, body=b'{"accepted":true}')
        )
        self.assertEqual(raw_response.status_code, 202)

    def test_discord_transport_executor_supports_typed_actions(self) -> None:
        callback_sender = MagicMock()
        client = MagicMock()
        executor = DiscordTransportExecutor(
            bot_token="token",
            interaction_callback_sender=callback_sender,
            client_factory=MagicMock(return_value=client),
        )

        executor.execute(
            action=DiscordInteractionResponseAction(
                interaction_id="i-1",
                interaction_token="tok-1",
                status_code=200,
                body=b"{}",
            )
        )
        executor.execute(
            action=DiscordChannelMessageAction(
                channel_id="c-1",
                content="hello",
            )
        )

        callback_sender.assert_called_once()
        client.post_message.assert_called_once_with(channel_id="c-1", content="hello", components=None)

    def test_discord_reply_transport_routes_methods_through_typed_actions(self) -> None:
        send_interaction_followup = MagicMock()
        send_thread_reply = MagicMock()
        send_ask_with_thread = MagicMock()
        send_seed_with_thread = MagicMock()
        transport = DiscordReplyTransport(
            send_interaction_followup=send_interaction_followup,
            send_thread_reply=send_thread_reply,
            send_ask_with_thread=send_ask_with_thread,
            send_seed_with_thread=send_seed_with_thread,
        )

        transport.send_interaction_followup(application_id="app", interaction_token="tok", content="hello")
        transport.send_thread_reply(
            session=object(),
            settings=object(),
            tenant=object(),
            channel_id="c1",
            reply_to_message_id="m1",
            content="thread",
        )
        transport.send_ask_with_thread(
            session=object(),
            settings=object(),
            tenant=object(),
            channel_id="c1",
            user_id="u1",
            content="ask",
        )
        transport.send_seed_with_thread(
            session=object(),
            settings=object(),
            tenant=object(),
            channel_id="c1",
            user_id="u1",
            content="seed",
            request_id="r1",
            questions=["q1"],
        )

        send_interaction_followup.assert_called_once()
        send_thread_reply.assert_called_once()
        send_ask_with_thread.assert_called_once()
        send_seed_with_thread.assert_called_once()

    def test_github_transport_executor_supports_reaction_actions(self) -> None:
        github_client = MagicMock()
        executor = GitHubTransportExecutor(github_client=github_client)

        executor.execute(
            action=GitHubIssueCommentReactionAction(
                repo_full_name="org/repo",
                comment_id=101,
                content="eyes",
            )
        )
        executor.execute(
            action=GitHubPullRequestReviewCommentReactionAction(
                repo_full_name="org/repo",
                comment_id=202,
                content="eyes",
            )
        )

        github_client.add_issue_comment_reaction.assert_called_once_with(
            repo_full_name="org/repo",
            comment_id=101,
            content="eyes",
        )
        github_client.add_pull_request_review_comment_reaction.assert_called_once_with(
            repo_full_name="org/repo",
            comment_id=202,
            content="eyes",
        )


if __name__ == "__main__":
    unittest.main()
