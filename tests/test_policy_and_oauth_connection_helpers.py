from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from orchestrator.api.jira_oauth.connection_service import resolve_tenant_jira_connection, tenant_jira_oauth_context
from orchestrator.core import project_policy
from orchestrator.core.communications import integration_contracts


class ProjectPolicyHelpersTests(unittest.TestCase):
    def test_normalize_project_policy_overrides(self) -> None:
        normalized = project_policy.normalize_project_policy_overrides(
            {
                "allow_jira_transitions": True,
                "allow_pr_creation": False,
                "allow_label_mutations": "nope",
                "allow_auto_merge": True,
                "max_dev_test_review_loops": 0,
                "max_pr_auto_remediation_loops": 0,
                "max_concurrent_runs": "bad",
                "allowed_commands": [" run ", "", "  ", "retry"],
                "require_agents_md": True,
                "ignored": "x",
            }
        )
        self.assertEqual(normalized["allow_jira_transitions"], True)
        self.assertEqual(normalized["allow_pr_creation"], False)
        self.assertEqual(normalized["allow_auto_merge"], True)
        self.assertNotIn("allow_label_mutations", normalized)
        self.assertEqual(normalized["max_dev_test_review_loops"], 1)
        self.assertEqual(normalized["max_pr_auto_remediation_loops"], 1)
        self.assertNotIn("max_concurrent_runs", normalized)
        self.assertEqual(normalized["allowed_commands"], ["run", "retry"])
        self.assertEqual(normalized["require_agents_md"], True)

    def test_resolve_effective_policy_caps_and_intersections(self) -> None:
        effective = project_policy.resolve_effective_policy(
            tenant_policy={
                "allow_jira_transitions": True,
                "allow_pr_creation": True,
                "allow_label_mutations": True,
                "allow_auto_merge": True,
                "max_dev_test_review_loops": 10,
                "max_pr_auto_remediation_loops": 5,
                "max_concurrent_runs": 8,
                "allowed_commands": ["run", "retry"],
                "require_agents_md": False,
            },
            project_overrides={
                "allow_pr_creation": False,
                "allow_auto_merge": False,
                "max_dev_test_review_loops": 999,
                "max_pr_auto_remediation_loops": 999,
                "max_concurrent_runs": 3,
                "allowed_commands": ["retry", "cancel"],
                "require_agents_md": True,
            },
        )
        self.assertEqual(effective["allow_pr_creation"], False)
        self.assertEqual(effective["allow_auto_merge"], False)
        self.assertEqual(effective["max_dev_test_review_loops"], 10)
        self.assertEqual(effective["max_pr_auto_remediation_loops"], 5)
        self.assertEqual(effective["max_concurrent_runs"], 3)
        self.assertEqual(effective["allowed_commands"], ["retry"])
        self.assertEqual(effective["require_agents_md"], True)


class JiraConnectionServiceTests(unittest.TestCase):
    def test_resolve_tenant_jira_connection_validation(self) -> None:
        session = MagicMock()
        tenant = SimpleNamespace(jira_config={})
        with self.assertRaises(HTTPException) as missing_ctx:
            resolve_tenant_jira_connection(session=session, tenant=tenant)
        self.assertEqual(missing_ctx.exception.status_code, 400)

        tenant = SimpleNamespace(jira_config={"connection_id": "conn-1"})
        session.get.return_value = None
        with self.assertRaises(HTTPException) as not_found_ctx:
            resolve_tenant_jira_connection(session=session, tenant=tenant)
        self.assertEqual(not_found_ctx.exception.status_code, 400)

    def test_tenant_jira_oauth_context(self) -> None:
        session = MagicMock()
        tenant = SimpleNamespace(jira_config={"connection_id": "conn-1"})
        connection = SimpleNamespace(connection_id="conn-1")
        session.get.return_value = connection
        settings = SimpleNamespace()

        with (
            patch("orchestrator.api.jira_oauth.connection_service.refresh_jira_connection_tokens", return_value="tok"),
            patch("orchestrator.api.jira_oauth.connection_service.jira_oauth_client", return_value="client"),
        ):
            context = tenant_jira_oauth_context(session=session, tenant=tenant, settings=settings)

        self.assertEqual(context.connection, connection)
        self.assertEqual(context.access_token, "tok")
        self.assertEqual(context.client, "client")


class IntegrationContractsTests(unittest.TestCase):
    def test_protocol_default_bodies_execute(self) -> None:
        # These calls exercise protocol method bodies so they are covered.
        self.assertIsNone(integration_contracts.InboundAdapter.verify(object(), headers={}, body=b""))
        self.assertIsNone(integration_contracts.InboundAdapter.parse(object(), headers={}, body=b""))
        self.assertIsNone(integration_contracts.OutboundAdapter.send(object(), event=object()))
        self.assertIsNone(
            integration_contracts.InteractiveReplyTransport.send_interaction_followup(
                object(),
                application_id="app",
                interaction_token="tok",
                content="hello",
            )
        )
        self.assertIsNone(
            integration_contracts.InteractiveReplyTransport.send_thread_reply(
                object(),
                session=object(),
                settings=object(),
                tenant=object(),
                channel_id="c1",
                reply_to_message_id="m1",
                content="hello",
            )
        )
        self.assertIsNone(
            integration_contracts.InteractiveReplyTransport.send_ask_with_thread(
                object(),
                session=object(),
                settings=object(),
                tenant=object(),
                channel_id="c1",
                user_id="u1",
                content="hello",
            )
        )
        self.assertIsNone(
            integration_contracts.InteractiveReplyTransport.send_seed_with_thread(
                object(),
                session=object(),
                settings=object(),
                tenant=object(),
                channel_id="c1",
                user_id="u1",
                content="hello",
                request_id="r1",
                questions=["q1"],
            )
        )


if __name__ == "__main__":
    unittest.main()
