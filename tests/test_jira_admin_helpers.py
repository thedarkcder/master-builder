from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from orchestrator.api.admin import (
    jira_oauth_helpers,
    jira_webhook_delete,
    jira_webhook_helpers,
    jira_webhook_response_helpers,
)
from orchestrator.api.schemas import JiraWebhookActionResult


class JiraWebhookHelpersTests(unittest.TestCase):
    def test_default_ready_jql_and_filter(self) -> None:
        ready_jql = jira_webhook_helpers.default_ready_jql(
            project_keys=["MAB", "YANA"],
            ready_statuses=["Ready", "Todo"],
        )
        self.assertIn('project in ("MAB", "YANA")', ready_jql)

        filter_jql = jira_webhook_helpers.jira_webhook_filter_jql({"project_keys": ["MAB", " YANA "]})
        self.assertEqual(filter_jql, 'project in ("MAB", "YANA") ORDER BY updated DESC')

        with self.assertRaises(ValueError):
            jira_webhook_helpers.jira_webhook_filter_jql({"project_keys": []})

    def test_managed_webhook_id_parsing(self) -> None:
        self.assertEqual(jira_webhook_helpers.parse_jira_webhook_id(10), 10)
        self.assertEqual(jira_webhook_helpers.parse_jira_webhook_id("42"), 42)
        self.assertIsNone(jira_webhook_helpers.parse_jira_webhook_id(""))
        self.assertIsNone(jira_webhook_helpers.parse_jira_webhook_id("abc"))
        self.assertEqual(
            jira_webhook_helpers.parse_managed_webhook_ids({"managed_webhook_ids": ["1", "x", 2, ""]}),
            [1, 2],
        )

    def test_error_helpers(self) -> None:
        limit_exc = Exception("Maximum of 5 webhooks is allowed per app per user")
        self.assertTrue(jira_webhook_helpers.is_jira_webhook_limit_error(limit_exc))
        single_url_exc = Exception("Only a single URL per user is allowed to be registered via REST API")
        self.assertTrue(jira_webhook_helpers.is_jira_webhook_single_url_error(single_url_exc))

        conflict = Exception("The currently used URL: https://example.com/hook)")
        self.assertEqual(
            jira_webhook_helpers.extract_jira_webhook_conflict_url(conflict),
            "https://example.com/hook",
        )
        self.assertIsNone(jira_webhook_helpers.extract_jira_webhook_conflict_url(Exception("no url")))

    def test_callback_url(self) -> None:
        settings = SimpleNamespace(public_api_base_url="https://api.example.com/")
        self.assertEqual(
            jira_webhook_helpers.jira_webhook_callback_url(settings=settings, tenant_id="route/25"),
            "https://api.example.com/jira/webhook/route%2F25",
        )


class JiraWebhookResponseHelpersTests(unittest.TestCase):
    def test_action_status_code(self) -> None:
        self.assertEqual(
            jira_webhook_response_helpers.jira_webhook_action_status_code(
                JiraWebhookActionResult(ok=True, action="provision", details="ok")
            ),
            200,
        )
        self.assertEqual(
            jira_webhook_response_helpers.jira_webhook_action_status_code(
                JiraWebhookActionResult(ok=False, action="x", details="Jira OAuth connection is not linked")
            ),
            400,
        )
        self.assertEqual(
            jira_webhook_response_helpers.jira_webhook_action_status_code(
                JiraWebhookActionResult(ok=False, action="x", details="Configured Jira connection was not found")
            ),
            400,
        )
        self.assertEqual(
            jira_webhook_response_helpers.jira_webhook_action_status_code(
                JiraWebhookActionResult(ok=False, action="x", details="other")
            ),
            502,
        )

    def test_build_diagnostics(self) -> None:
        now = datetime.now(timezone.utc)
        recent = (now - timedelta(minutes=5)).isoformat()
        diagnostics = jira_webhook_response_helpers.build_jira_webhook_diagnostics(
            tenant_id="route25",
            within_minutes=10,
            jira_config={
                "connection_id": "conn-1",
                "managed_webhook_ids": ["1", "2"],
                "webhook_last_received_at": recent,
                "webhook_last_delivery_id": "d1",
            },
            settings=SimpleNamespace(),
            jira_webhook_callback_url_fn=MagicMock(return_value="https://api/hook"),
            parse_managed_webhook_ids_fn=jira_webhook_helpers.parse_managed_webhook_ids,
        )
        self.assertTrue(diagnostics.connected)
        self.assertTrue(diagnostics.recent_delivery_ok)
        self.assertEqual(diagnostics.managed_webhook_ids, [1, 2])

        stale = jira_webhook_response_helpers.build_jira_webhook_diagnostics(
            tenant_id="route25",
            within_minutes=1,
            jira_config={"webhook_last_received_at": "invalid"},
            settings=SimpleNamespace(),
            jira_webhook_callback_url_fn=MagicMock(return_value="https://api/hook"),
            parse_managed_webhook_ids_fn=MagicMock(return_value=[]),
        )
        self.assertFalse(stale.connected)
        self.assertFalse(stale.recent_delivery_ok)


class JiraOAuthHelpersTests(unittest.TestCase):
    def test_resolve_secret_ref(self) -> None:
        session = MagicMock()
        settings = SimpleNamespace(secrets_encryption_key="k")
        with patch("orchestrator.api.admin.jira_oauth_helpers.resolve_scoped_secret_ref", return_value="value"):
            self.assertEqual(
                jira_oauth_helpers.resolve_secret_ref(session, ref_name="ref", settings=settings, tenant_id="t"),
                "value",
            )

        with patch("orchestrator.api.admin.jira_oauth_helpers.resolve_scoped_secret_ref", return_value=""):
            with self.assertRaises(ValueError):
                jira_oauth_helpers.resolve_secret_ref(session, ref_name="ref", settings=settings)

    def test_jira_oauth_client(self) -> None:
        session = MagicMock()
        settings = SimpleNamespace(
            jira_oauth_client_id_ref="id_ref",
            jira_oauth_client_secret_ref="secret_ref",
            public_api_base_url="https://api.example.com/",
        )
        with (
            patch(
                "orchestrator.api.admin.jira_oauth_helpers.resolve_secret_ref",
                side_effect=["cid", "csecret"],
            ),
            patch("orchestrator.api.admin.jira_oauth_helpers.JiraOAuthClient", return_value=MagicMock()) as client_cls,
        ):
            client = jira_oauth_helpers.jira_oauth_client(session=session, settings=settings, tenant_id="t")

        config = client_cls.call_args.args[0]
        self.assertEqual(config.client_id, "cid")
        self.assertEqual(config.client_secret, "csecret")
        self.assertEqual(config.redirect_uri, "https://api.example.com/api/admin/jira/connect/callback")
        self.assertIsNotNone(client)

    def test_refresh_tokens_paths(self) -> None:
        session = MagicMock()
        now = datetime.now(timezone.utc)
        settings = SimpleNamespace(secrets_encryption_key="k")

        connection = SimpleNamespace(
            access_token_expires_at=now + timedelta(minutes=5),
            access_token_encrypted="enc-access",
            refresh_token_encrypted="enc-refresh",
            scopes=["read:jira-user"],
            updated_at=now,
        )

        with patch("orchestrator.api.admin.jira_oauth_helpers.decrypt_value", return_value="current-token"):
            token = jira_oauth_helpers.refresh_jira_connection_tokens(
                session,
                connection=connection,
                settings=settings,
            )
        self.assertEqual(token, "current-token")

        expired_connection = SimpleNamespace(
            access_token_expires_at=now,
            access_token_encrypted="enc-access",
            refresh_token_encrypted="enc-refresh",
            scopes=["old"],
            updated_at=now,
        )
        token_set = SimpleNamespace(
            access_token="new-access",
            refresh_token="new-refresh",
            expires_at=now + timedelta(hours=1),
            scopes=["new"],
        )
        client = MagicMock()
        client.refresh_tokens.return_value = token_set
        with (
            patch("orchestrator.api.admin.jira_oauth_helpers.decrypt_value", return_value="refresh"),
            patch("orchestrator.api.admin.jira_oauth_helpers.encrypt_value", side_effect=["enc-new-access", "enc-new-refresh"]),
        ):
            token = jira_oauth_helpers.refresh_jira_connection_tokens(
                session,
                connection=expired_connection,
                settings=settings,
                tenant_id="route25",
                jira_oauth_client_fn=MagicMock(return_value=client),
            )

        self.assertEqual(token, "new-access")
        self.assertEqual(expired_connection.access_token_encrypted, "enc-new-access")
        self.assertEqual(expired_connection.refresh_token_encrypted, "enc-new-refresh")
        self.assertEqual(expired_connection.scopes, ["new"])
        session.commit.assert_called()


class JiraWebhookDeleteTests(unittest.TestCase):
    def test_delete_webhooks_paths(self) -> None:
        session = MagicMock()
        tenant = SimpleNamespace(tenant_id="route25", jira_config={})

        ok, details, ids = jira_webhook_delete.delete_jira_webhooks(
            session=session,
            tenant=tenant,
            settings=SimpleNamespace(),
            parse_managed_webhook_ids_fn=MagicMock(return_value=[]),
            refresh_jira_connection_tokens_fn=MagicMock(),
            jira_oauth_client_fn=MagicMock(),
        )
        self.assertTrue(ok)
        self.assertEqual(ids, [])
        self.assertIn("No Jira connection linked", details)

        tenant.jira_config = {"connection_id": "conn-1", "managed_webhook_ids": [1, 2]}
        session.get.return_value = None
        ok, details, ids = jira_webhook_delete.delete_jira_webhooks(
            session=session,
            tenant=tenant,
            settings=SimpleNamespace(),
            parse_managed_webhook_ids_fn=jira_webhook_helpers.parse_managed_webhook_ids,
            refresh_jira_connection_tokens_fn=MagicMock(),
            jira_oauth_client_fn=MagicMock(),
        )
        self.assertFalse(ok)
        self.assertEqual(ids, [1, 2])
        self.assertIn("not found", details)

    def test_delete_webhooks_success_and_error(self) -> None:
        session = MagicMock()
        tenant = SimpleNamespace(tenant_id="route25", jira_config={"connection_id": "conn-1", "managed_webhook_ids": [1]})
        connection = SimpleNamespace(cloud_id="cloud")
        session.get.return_value = connection

        client = MagicMock()
        ok, details, ids = jira_webhook_delete.delete_jira_webhooks(
            session=session,
            tenant=tenant,
            settings=SimpleNamespace(),
            parse_managed_webhook_ids_fn=jira_webhook_helpers.parse_managed_webhook_ids,
            refresh_jira_connection_tokens_fn=MagicMock(return_value="token"),
            jira_oauth_client_fn=MagicMock(return_value=client),
        )
        self.assertTrue(ok)
        self.assertEqual(ids, [1])
        self.assertIn("Deleted 1 Jira webhook", details)

        client.delete_webhooks.side_effect = ValueError("boom")
        tenant.jira_config = {"connection_id": "conn-1", "managed_webhook_ids": [1]}
        ok, details, ids = jira_webhook_delete.delete_jira_webhooks(
            session=session,
            tenant=tenant,
            settings=SimpleNamespace(),
            parse_managed_webhook_ids_fn=jira_webhook_helpers.parse_managed_webhook_ids,
            refresh_jira_connection_tokens_fn=MagicMock(return_value="token"),
            jira_oauth_client_fn=MagicMock(return_value=client),
        )
        self.assertFalse(ok)
        self.assertEqual(ids, [1])
        self.assertIn("Failed to delete Jira webhooks", details)


if __name__ == "__main__":
    unittest.main()
