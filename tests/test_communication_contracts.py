from __future__ import annotations

import json
from dataclasses import asdict
import unittest
from datetime import datetime, timezone

from orchestrator.core.communications.contracts import (
    ActorIdentity,
    CommandRequest,
    CommunicationAction,
    CommunicationEvent,
    CommunicationLink,
    InboundMessage,
    ProjectScope,
)


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
        self.assertIn('"label": "Issue"', snapshot)


if __name__ == "__main__":
    unittest.main()
