from __future__ import annotations

import json
from contextlib import nullcontext
from dataclasses import asdict
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from orchestrator.api.discord.interactions import followup as followup_module
from orchestrator.core.communications.contracts import (
    ActorIdentity,
    CommandRequest,
    CommunicationAction,
    CommunicationEvent,
    CommunicationLink,
    DiscordAskWithThreadAction,
    DiscordChannelMessageAction,
    DiscordChannelMessageWithAttachmentAction,
    DiscordInteractionResponseAction,
    DiscordSeedWithThreadAction,
    DiscordTenantNotificationAction,
    DiscordThreadReplyAction,
    GitHubInlineReviewBatchAction,
    GitHubIssueCommentReactionAction,
    GitHubManualFixIssueCommentReplyAction,
    GitHubManualFixReviewThreadReplyAction,
    GitHubPullRequestMergeAction,
    GitHubPullRequestReactionAction,
    GitHubPullRequestReviewCommentReactionAction,
    GitHubStickyReviewCommentAction,
    HttpJsonResponseAction,
    HttpJsonResponseBytesAction,
    InboundMessage,
    IngressResult,
    ProjectScope,
    TransportEnvelope,
)
from orchestrator.api.transport_runtime import (
    HttpTransportExecutor,
    execute_side_effect_action,
    execute_http_ingress_result,
    execute_side_effect_ingress_result,
)
from orchestrator.core.discord.transport_executor import DiscordTransportExecutor
from orchestrator.core.github.transport_executor import GitHubTransportExecutor


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
            links=(
                CommunicationLink(
                    label="Issue", url="https://example.atlassian.net/browse/TP-10"
                ),
            ),
            actions=(
                CommunicationAction(action_id="reply", label="Reply", kind="button"),
            ),
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
                links=(
                    CommunicationLink(label="Issue", url="https://example/browse/TP-1"),
                ),
                actions=(
                    CommunicationAction(
                        action_id="reply", label="Reply", kind="button"
                    ),
                ),
                correlation_id=inbound.correlation_id,
            )
        )
        snapshot = json.dumps(payload, sort_keys=True, default=str)
        self.assertIn('"event_type": "run_started"', snapshot)
        self.assertIn('"action_id": "reply"', snapshot)

    def test_http_transport_executor_supports_typed_http_actions(self) -> None:
        executor = HttpTransportExecutor()
        json_response = executor.execute(
            action=HttpJsonResponseAction(status_code=200, content={"ok": True})
        )
        self.assertEqual(json_response.status_code, 200)

        raw_response = executor.execute(
            action=HttpJsonResponseBytesAction(
                status_code=202, body=b'{"accepted":true}'
            )
        )
        self.assertEqual(raw_response.status_code, 202)

    def test_http_ingress_executes_side_effect_actions_before_returning_response(
        self,
    ) -> None:
        side_effect_executor = MagicMock()
        response = execute_http_ingress_result(
            result=IngressResult(
                actions=(
                    GitHubIssueCommentReactionAction(
                        repo_full_name="org/repo",
                        comment_id=101,
                        content="eyes",
                    ),
                    HttpJsonResponseAction(status_code=202, content={"accepted": True}),
                )
            ),
            transport_action_executors=(side_effect_executor,),
        )
        self.assertEqual(response.status_code, 202)
        side_effect_executor.execute.assert_called_once()

    def test_http_ingress_logs_contract_lifecycle_events(self) -> None:
        side_effect_executor = MagicMock()
        with self.assertLogs(
            "orchestrator.api.transport_runtime", level="INFO"
        ) as captured:
            response = execute_http_ingress_result(
                result=IngressResult(
                    actions=(
                        GitHubIssueCommentReactionAction(
                            repo_full_name="org/repo",
                            comment_id=101,
                            content="eyes",
                        ),
                        HttpJsonResponseAction(
                            status_code=202, content={"accepted": True}
                        ),
                    )
                ),
                envelope=TransportEnvelope(
                    transport="github_webhook",
                    event_type="issue_comment",
                    request_id="req-1",
                    tenant_id_hint="example-workspace",
                ),
                transport_action_executors=(side_effect_executor,),
            )
        self.assertEqual(response.status_code, 202)
        joined = "\n".join(captured.output)
        self.assertIn("transport_ingress_result_built", joined)
        self.assertIn("transport_action_executed", joined)
        self.assertIn("transport_http_response_selected", joined)

    def test_side_effect_ingress_logs_contract_lifecycle_events(self) -> None:
        side_effect_executor = MagicMock()
        with self.assertLogs(
            "orchestrator.api.transport_runtime", level="INFO"
        ) as captured:
            execute_side_effect_ingress_result(
                result=IngressResult(
                    actions=(
                        GitHubIssueCommentReactionAction(
                            repo_full_name="org/repo",
                            comment_id=101,
                            content="eyes",
                        ),
                    )
                ),
                envelope=TransportEnvelope(
                    transport="discord_gateway",
                    event_type="message_create",
                    request_id="req-2",
                    tenant_id_hint="example-workspace",
                ),
                transport_action_executors=(side_effect_executor,),
            )
        joined = "\n".join(captured.output)
        self.assertIn("transport_ingress_result_built", joined)
        self.assertIn("transport_action_executed", joined)

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
        client.post_message.assert_called_once_with(
            channel_id="c-1", content="hello", components=None
        )

    def test_discord_transport_executor_returns_message_metadata_for_attachment_action(
        self,
    ) -> None:
        client = MagicMock()
        client.post_message_with_attachment.return_value = {"id": "discord-msg-1"}
        executor = DiscordTransportExecutor(
            bot_token="token",
            client_factory=MagicMock(return_value=client),
        )

        result = executor.execute(
            action=DiscordChannelMessageWithAttachmentAction(
                channel_id="c-1",
                content="hello",
                filename="voice.wav",
                file_bytes=b"wav",
                content_type="audio/wav",
            )
        )

        self.assertEqual(result, {"message_id": "discord-msg-1", "channel_id": "c-1"})

    def test_execute_side_effect_action_returns_executor_metadata(self) -> None:
        executor = MagicMock()
        executor.execute.return_value = {
            "message_id": "discord-msg-1",
            "channel_id": "c-1",
        }

        result = execute_side_effect_action(
            action=GitHubIssueCommentReactionAction(
                repo_full_name="org/repo",
                comment_id=101,
                content="eyes",
            ),
            envelope=TransportEnvelope(
                transport="discord_gateway",
                event_type="message_create",
                request_id="req-3",
            ),
            transport_action_executors=(executor,),
        )

        self.assertEqual(result, {"message_id": "discord-msg-1", "channel_id": "c-1"})

    def test_discord_transport_executor_delegates_thread_style_actions_to_handler(
        self,
    ) -> None:
        handler = MagicMock()
        executor = DiscordTransportExecutor(
            interaction_followup_sender=MagicMock(),
            thread_action_handler=handler,
        )

        executor.execute(
            action=DiscordThreadReplyAction(
                tenant_id="tenant-1",
                channel_id="c1",
                reply_to_message_id="m1",
                content="thread",
            )
        )
        executor.execute(
            action=DiscordAskWithThreadAction(
                tenant_id="tenant-1",
                channel_id="c1",
                user_id="u1",
                content="ask",
            )
        )
        executor.execute(
            action=DiscordSeedWithThreadAction(
                tenant_id="tenant-1",
                channel_id="c1",
                user_id="u1",
                content="seed",
                request_id="r1",
                questions=["q1"],
            )
        )

        handler.execute_thread_reply.assert_called_once()
        handler.execute_ask_with_thread.assert_called_once()
        handler.execute_seed_with_thread.assert_called_once()

    def test_discord_transport_executor_delegates_tenant_notification_action_to_handler(
        self,
    ) -> None:
        notification_handler = MagicMock()
        executor = DiscordTransportExecutor(
            notification_action_handler=notification_handler,
        )

        executor.execute(
            action=DiscordTenantNotificationAction(
                tenant_id="tenant-1",
                project_id="project-1",
                message="hello",
                event="pr_review_gate",
            )
        )

        notification_handler.execute_tenant_notification.assert_called_once()

    def test_discord_transport_executor_executes_concrete_thread_senders_with_injected_dependencies(
        self,
    ) -> None:
        session = MagicMock()
        tenant = SimpleNamespace(tenant_id="tenant-1", is_enabled=True)
        session.get.return_value = tenant
        settings = SimpleNamespace()
        executor = DiscordTransportExecutor(
            session_factory=lambda: nullcontext(session),
            settings_factory=lambda: settings,
            thread_followup_sender=followup_module._send_discord_thread_followup,
            ask_with_thread_sender=followup_module._send_discord_ask_response_with_thread,
            seed_with_thread_sender=followup_module._send_discord_seed_followup_with_thread,
        )

        with (
            patch.object(
                followup_module, "_send_discord_thread_followup_impl"
            ) as thread_impl,
            patch.object(
                followup_module, "_send_discord_ask_response_with_thread_impl"
            ) as ask_impl,
            patch.object(
                followup_module, "_send_discord_seed_followup_with_thread_impl"
            ) as seed_impl,
        ):
            executor.execute(
                action=DiscordThreadReplyAction(
                    tenant_id="tenant-1",
                    channel_id="c1",
                    reply_to_message_id="m1",
                    content="thread",
                )
            )
            executor.execute(
                action=DiscordAskWithThreadAction(
                    tenant_id="tenant-1",
                    channel_id="c1",
                    user_id="u1",
                    content="ask",
                    issue_key="MAB-174",
                )
            )
            executor.execute(
                action=DiscordSeedWithThreadAction(
                    tenant_id="tenant-1",
                    channel_id="c1",
                    user_id="u1",
                    content="seed",
                    request_id="r1",
                    questions=["q1"],
                )
            )

        self.assertIsNotNone(thread_impl.call_args.kwargs["discord_api_client_fn"])
        self.assertIsNotNone(ask_impl.call_args.kwargs["discord_api_client_fn"])
        self.assertEqual(ask_impl.call_args.kwargs["issue_key"], "MAB-174")
        self.assertIsNotNone(seed_impl.call_args.kwargs["discord_api_client_fn"])

    def test_github_transport_executor_supports_reaction_actions(self) -> None:
        github_client = MagicMock()
        executor = GitHubTransportExecutor(
            github_client=github_client, session=MagicMock()
        )

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
        executor.execute(
            action=GitHubPullRequestReactionAction(
                repo_full_name="org/repo",
                pr_number=12,
                content="confused",
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
        github_client.sync_pull_request_reaction.assert_called_once_with(
            repo_full_name="org/repo",
            pr_number=12,
            content="confused",
        )

    def test_github_transport_executor_supports_publication_actions(self) -> None:
        github_client = MagicMock()
        session = MagicMock()
        executor = GitHubTransportExecutor(github_client=github_client, session=session)

        with (
            patch(
                "orchestrator.core.github.transport_executor.upsert_sticky_review_comment"
            ) as sticky_review,
            patch(
                "orchestrator.core.github.transport_executor.publish_inline_review_batch"
            ) as inline_review,
            patch(
                "orchestrator.core.github.transport_executor.upsert_manual_fix_issue_comment_reply"
            ) as manual_fix_issue_reply,
            patch(
                "orchestrator.core.github.transport_executor.upsert_manual_fix_review_thread_reply"
            ) as manual_fix_reply,
        ):
            executor.execute(
                action=GitHubStickyReviewCommentAction(
                    request_id="req-1",
                    repo_full_name="org/repo",
                    pr_number=11,
                    tenant_id="tenant-1",
                    project_id="project-1",
                    head_sha="abc123",
                    signal=MagicMock(),
                    findings_result=MagicMock(),
                    event="pull_request",
                    action_name="synchronize",
                )
            )
            executor.execute(
                action=GitHubInlineReviewBatchAction(
                    request_id="req-1",
                    repo_full_name="org/repo",
                    pr_number=11,
                    head_sha="abc123",
                    tenant_id="tenant-1",
                    project_id="project-1",
                    findings=(),
                    changed_paths={"a.py"},
                )
            )
            executor.execute(
                action=GitHubManualFixReviewThreadReplyAction(
                    repo_full_name="org/repo",
                    pr_number=11,
                    tenant_id="tenant-1",
                    project_id="project-1",
                    triggering_comment_id=99,
                    requested_by="alice",
                    triggering_comment_url="https://github.com/comment",
                    instruction_text="fix the flaky test",
                    issue_key="GP-1",
                    issue_url="https://jira/GP-1",
                    enqueued=True,
                    run_id="run-1",
                    reason=None,
                )
            )
            executor.execute(
                action=GitHubManualFixIssueCommentReplyAction(
                    repo_full_name="org/repo",
                    pr_number=11,
                    tenant_id="tenant-1",
                    project_id="project-1",
                    triggering_comment_id=101,
                    requested_by="alice",
                    triggering_comment_url="https://github.com/comment",
                    instruction_text="fix the flaky test",
                    issue_key="GP-1",
                    issue_url="https://jira/GP-1",
                    enqueued=True,
                    run_id="run-1",
                    reason=None,
                )
            )
            executor.execute(
                action=GitHubPullRequestMergeAction(
                    repo_full_name="org/repo",
                    pr_number=11,
                    head_sha="abc123",
                )
            )

        sticky_review.assert_called_once()
        inline_review.assert_called_once()
        manual_fix_reply.assert_called_once()
        manual_fix_issue_reply.assert_called_once()

        github_client.merge_pull_request.assert_called_once_with(
            repo_full_name="org/repo",
            pr_number=11,
            head_sha="abc123",
        )


if __name__ == "__main__":
    unittest.main()
