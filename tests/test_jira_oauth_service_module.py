from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from orchestrator.api.jira_oauth.service import (
    jira_oauth_client,
    refresh_jira_connection_tokens,
    resolve_secret_ref,
)


class JiraOauthServiceModuleTests(unittest.TestCase):
    def test_resolve_secret_ref(self) -> None:
        session = MagicMock()
        settings = SimpleNamespace(secrets_encryption_key="enc")

        with patch(
            "orchestrator.api.jira_oauth.service.resolve_platform_secret_ref",
            return_value="value",
        ) as resolve_secret_mock:
            self.assertEqual(resolve_secret_ref(session, ref_name="REF", settings=settings), "value")
        resolve_secret_mock.assert_called_once()
        self.assertEqual(resolve_secret_mock.call_args.kwargs["secret_ref"], "REF")

        with patch("orchestrator.api.jira_oauth.service.resolve_platform_secret_ref", return_value=""):
            with self.assertRaisesRegex(ValueError, "Missing secret value"):
                resolve_secret_ref(session, ref_name="REF", settings=settings)

    def test_jira_oauth_client_uses_secret_refs(self) -> None:
        session = MagicMock()
        settings = SimpleNamespace(
            jira_oauth_client_id_ref="ID_REF",
            jira_oauth_client_secret_ref="SECRET_REF",
            public_api_base_url="https://api.example.com/",
        )

        with (
            patch("orchestrator.api.jira_oauth.service.resolve_secret_ref", side_effect=["client-id", "client-secret"]),
            patch("orchestrator.api.jira_oauth.service.JiraOAuthClient") as client_cls,
        ):
            jira_oauth_client(session=session, settings=settings, tenant_id="t1", project_id="p1")

        config = client_cls.call_args.args[0]
        self.assertEqual(config.client_id, "client-id")
        self.assertEqual(config.client_secret, "client-secret")
        self.assertEqual(config.redirect_uri, "https://api.example.com/api/admin/jira/connect/callback")

    def test_refresh_jira_connection_tokens_fast_path_and_refresh_path(self) -> None:
        session = MagicMock()
        settings = SimpleNamespace(secrets_encryption_key="enc")
        future_expiry = datetime.now(timezone.utc) + timedelta(minutes=10)
        connection = SimpleNamespace(
            connection_id="conn-1",
            access_token_expires_at=future_expiry,
            access_token_encrypted="enc-access",
            refresh_token_encrypted="enc-refresh",
            scopes=["read:jira-work"],
            updated_at=None,
        )

        with patch("orchestrator.api.jira_oauth.service.decrypt_secret_value", return_value="cached-token") as decrypt_mock:
            token = refresh_jira_connection_tokens(session, connection=connection, settings=settings, tenant_id="t1")
        self.assertEqual(token, "cached-token")
        decrypt_mock.assert_called_once()
        self.assertEqual(
            decrypt_mock.call_args.kwargs["context"],
            {"kind": "jira_oauth_token", "connection_id": connection.connection_id, "token_field": "access_token"},
        )

        expired_connection = SimpleNamespace(
            connection_id="conn-1",
            access_token_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            access_token_encrypted="enc-access",
            refresh_token_encrypted="enc-refresh",
            scopes=[],
            updated_at=None,
        )
        token_set = SimpleNamespace(
            access_token="new-access",
            refresh_token="new-refresh",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            scopes=["scope-1"],
        )
        client = MagicMock()
        client.refresh_tokens.return_value = token_set

        with (
            patch("orchestrator.api.jira_oauth.service.jira_oauth_client", return_value=client),
            patch("orchestrator.api.jira_oauth.service.decrypt_secret_value", return_value="refresh-token"),
            patch(
                "orchestrator.api.jira_oauth.service.encrypt_secret_value",
                side_effect=lambda *, plaintext, settings, encryption_key="", context=None: f"enc::{plaintext}",
            ),
        ):
            refreshed_token = refresh_jira_connection_tokens(
                session,
                connection=expired_connection,
                settings=settings,
                tenant_id="t1",
            )

        self.assertEqual(refreshed_token, "new-access")
        self.assertEqual(expired_connection.access_token_encrypted, "enc::new-access")
        self.assertEqual(expired_connection.refresh_token_encrypted, "enc::new-refresh")
        self.assertEqual(expired_connection.scopes, ["scope-1"])
        session.commit.assert_called_once()

    def test_refresh_jira_connection_tokens_normalizes_naive_expiry(self) -> None:
        session = MagicMock()
        settings = SimpleNamespace(secrets_encryption_key="enc")
        future_expiry = datetime.now() + timedelta(minutes=10)
        connection = SimpleNamespace(
            connection_id="conn-1",
            access_token_expires_at=future_expiry,
            access_token_encrypted="enc-access",
            refresh_token_encrypted="enc-refresh",
            scopes=["read:jira-work"],
            updated_at=None,
        )

        with patch("orchestrator.api.jira_oauth.service.decrypt_secret_value", return_value="cached-token"):
            token = refresh_jira_connection_tokens(session, connection=connection, settings=settings, tenant_id="t1")

        self.assertEqual(token, "cached-token")


if __name__ == "__main__":
    unittest.main()
