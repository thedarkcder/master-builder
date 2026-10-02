from __future__ import annotations

import secrets

from datetime import datetime, timezone

from cryptography.fernet import Fernet
from sqlalchemy import func, select
from fastapi.testclient import TestClient

from orchestrator.api.main import create_app
from orchestrator.core.config import get_settings
from orchestrator.core.platform.tenant_secret_service import tenant_secret_service
from orchestrator.core.platform.secret_service import platform_secret_service
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import Project, Tenant, WebhookJob
from tests.test_support.db_harness import SqliteTemplateApiTestCase


class WebhookAuthenticationSecurityTests(SqliteTemplateApiTestCase):
    @classmethod
    def class_environment_overrides(cls) -> dict[str, str]:
        return {
            "ORCHESTRATOR_ALLOW_SQLITE_FOR_TESTS": "true",
            "ORCHESTRATOR_SECRETS_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        }

    def setUp(self) -> None:
        self.database_url = self._start_test_database(name_prefix="webhook-auth")
        self.client = TestClient(create_app())
        self.session_factory = create_session_factory()
        self.webhook_token = secrets.token_urlsafe(32)
        with self.session_factory() as session:
            session.add(
                Tenant(
                    tenant_id="public-security",
                    name="Public Security Tests",
                    is_enabled=True,
                    jira_config={},
                    github_config={},
                    discord_config={},
                    repos_config={},
                    policy_config={},
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )
            )
            session.commit()

    def tearDown(self) -> None:
        self.client.close()
        self._cleanup_test_database()

    def _configure_discord_secret(self, *, store_secret: bool) -> None:
        with self.session_factory() as session:
            tenant = session.get(Tenant, "public-security")
            assert tenant is not None
            tenant.discord_config = {"command_secret_ref": "discord-command-token"}
            if store_secret:
                tenant_secret_service.upsert_secret(
                    session=session,
                    secret_ref="discord-command-token",
                    plaintext_value=self.webhook_token,
                    encryption_key=get_settings().secrets_encryption_key,
                    tenant_id="public-security",
                )
            session.commit()

    def _assert_queue_empty(self) -> None:
        with self.session_factory() as session:
            self.assertEqual(
                session.scalar(select(func.count()).select_from(WebhookJob)), 0
            )

    def test_discord_command_reference_survives_public_configuration_updates(
        self,
    ) -> None:
        self._configure_discord_secret(store_secret=True)
        settings = get_settings()
        auth = (settings.admin_username, settings.admin_password)
        endpoint = "/api/admin/tenants/public-security/discord"
        configured = self.client.patch(
            endpoint,
            json={"discord": {"command_secret_ref": "discord-command-token"}},
            auth=auth,
        )
        self.assertEqual(configured.status_code, 200, configured.text)
        self.assertEqual(
            configured.json()["discord"]["command_secret_ref"],
            "discord-command-token",
        )
        updated = self.client.patch(
            endpoint, json={"discord": {"channel_id": "test-channel"}}, auth=auth
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(
            updated.json()["discord"]["command_secret_ref"], "discord-command-token"
        )
        response = self.client.post(
            "/discord/webhook/public-security",
            json={"user_id": "example-member", "command": "!ask status"},
            headers={"X-Webhook-Token": self.webhook_token},
        )
        self.assertEqual(response.status_code, 200, response.text)

    def test_discord_command_reference_explicit_clear_rejects_commands(self) -> None:
        self._configure_discord_secret(store_secret=True)
        settings = get_settings()
        cleared = self.client.patch(
            "/api/admin/tenants/public-security/discord",
            json={"discord": {"command_secret_ref": None}},
            auth=(settings.admin_username, settings.admin_password),
        )
        self.assertEqual(cleared.status_code, 200, cleared.text)
        self.assertIn("command_secret_ref", cleared.json()["discord"])
        self.assertIsNone(cleared.json()["discord"]["command_secret_ref"])
        unrelated = self.client.patch(
            "/api/admin/tenants/public-security/discord",
            json={"discord": {"channel_id": "test-channel"}},
            auth=(settings.admin_username, settings.admin_password),
        )
        self.assertEqual(unrelated.status_code, 200, unrelated.text)
        self.assertIn("command_secret_ref", unrelated.json()["discord"])
        self.assertIsNone(unrelated.json()["discord"]["command_secret_ref"])
        response = self.client.post(
            "/discord/webhook/public-security",
            json={"user_id": "example-member", "command": "!ask status"},
            headers={"X-Webhook-Token": self.webhook_token},
        )
        self.assertEqual(response.status_code, 500)
        self._assert_queue_empty()

    def test_discord_command_reference_rejects_blank_and_whitespace(self) -> None:
        settings = get_settings()
        for invalid in ("", " ", " ref "):
            with self.subTest(invalid=invalid):
                response = self.client.patch(
                    "/api/admin/tenants/public-security/discord",
                    json={"discord": {"command_secret_ref": invalid}},
                    auth=(settings.admin_username, settings.admin_password),
                )
                self.assertEqual(response.status_code, 422, response.text)

    def _configure_coolify_secret(self) -> None:
        with self.session_factory() as session:
            tenant = session.get(Tenant, "public-security")
            assert tenant is not None
            tenant.deployment_plane_config = {
                "provider": "internal_coolify",
                "secret_refs": {"coolify_webhook_token": "coolify-webhook-token"},
            }
            session.add(
                Project(
                    project_id="public-security-project",
                    tenant_id=tenant.tenant_id,
                    name="Security Project",
                    github_repository="example/security",
                    jira_project_key="SECURITY",
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )
            )
            tenant_secret_service.upsert_secret(
                session=session,
                secret_ref="coolify-webhook-token",
                plaintext_value=self.webhook_token,
                encryption_key=get_settings().secrets_encryption_key,
                tenant_id=tenant.tenant_id,
            )
            session.commit()

    def test_coolify_wrong_token_cannot_enqueue_but_valid_token_can(self) -> None:
        self._configure_coolify_secret()
        endpoint = (
            "/deployments/coolify/webhook/public-security/public-security-project/"
        )
        rejected = self.client.post(
            endpoint + "WRONG_TEST_TOKEN", json={"status": "success"}
        )
        self.assertEqual(rejected.status_code, 401, rejected.text)
        self._assert_queue_empty()
        accepted = self.client.post(
            endpoint + self.webhook_token, json={"status": "success"}
        )
        self.assertEqual(accepted.status_code, 202, accepted.text)
        with self.session_factory() as session:
            self.assertEqual(
                session.scalar(select(func.count()).select_from(WebhookJob)), 1
            )

    def test_coolify_unknown_tenant_cannot_enqueue(self) -> None:
        rejected = self.client.post(
            "/deployments/coolify/webhook/missing-tenant/missing-project/WRONG_TEST_TOKEN",
            json={"status": "success"},
        )
        self.assertEqual(rejected.status_code, 404, rejected.text)
        self._assert_queue_empty()

    def test_discord_missing_secret_reference_cannot_enqueue_impersonated_command(
        self,
    ) -> None:
        response = self.client.post(
            "/discord/webhook/public-security",
            json={"user_id": "spoofed-member", "command": "!ask confidential status"},
        )
        self.assertEqual(response.status_code, 500)
        self._assert_queue_empty()

    def test_discord_missing_secret_value_cannot_enqueue_command(self) -> None:
        self._configure_discord_secret(store_secret=False)
        response = self.client.post(
            "/discord/webhook/public-security",
            json={"user_id": "spoofed-member", "command": "!ask status"},
        )
        self.assertEqual(response.status_code, 500)
        self._assert_queue_empty()

    def test_discord_authenticated_command_enqueues_but_wrong_token_does_not(
        self,
    ) -> None:
        self._configure_discord_secret(store_secret=True)
        payload = {"user_id": "example-member", "command": "!ask status"}
        rejected = self.client.post(
            "/discord/webhook/public-security",
            json=payload,
            headers={"X-Webhook-Token": "WRONG_TEST_TOKEN"},
        )
        self.assertEqual(rejected.status_code, 401)
        self._assert_queue_empty()
        accepted = self.client.post(
            "/discord/webhook/public-security",
            json=payload,
            headers={"X-Webhook-Token": self.webhook_token},
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        with self.session_factory() as session:
            self.assertEqual(
                session.scalar(select(func.count()).select_from(WebhookJob)), 1
            )

    def test_jira_missing_secret_reference_cannot_enqueue_forged_event(self) -> None:
        response = self.client.post(
            "/jira/webhook/public-security",
            json={
                "webhookEvent": "jira:issue_updated",
                "issue": {"key": "EXAMPLE-1", "fields": {}},
            },
        )
        self.assertEqual(response.status_code, 500)
        self._assert_queue_empty()

    def test_github_missing_platform_secret_cannot_accept_even_a_ping(self) -> None:
        response = self.client.post(
            "/github/webhook", json={}, headers={"X-GitHub-Event": "ping"}
        )
        self.assertEqual(response.status_code, 500)
        self._assert_queue_empty()

    def test_github_ping_requires_valid_signature_from_platform_secret(self) -> None:
        import hashlib
        import hmac

        with self.session_factory() as session:
            platform_secret_service.upsert_secret(
                session=session,
                secret_ref="platform/GITHUB_WEBHOOK_SECRET",
                plaintext_value=self.webhook_token,
                encryption_key=get_settings().secrets_encryption_key,
            )
            session.commit()
        rejected = self.client.post(
            "/github/webhook",
            json={},
            headers={
                "X-GitHub-Event": "ping",
                "X-Hub-Signature-256": "sha256=WRONG_TEST_SIGNATURE",
            },
        )
        self.assertEqual(rejected.status_code, 401)
        self._assert_queue_empty()
        body = b"{}"
        signature = (
            "sha256="
            + hmac.new(self.webhook_token.encode(), body, hashlib.sha256).hexdigest()
        )
        accepted = self.client.post(
            "/github/webhook",
            content=body,
            headers={"X-GitHub-Event": "ping", "X-Hub-Signature-256": signature},
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self._assert_queue_empty()
