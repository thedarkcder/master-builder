import asyncio
import os
import json
import hmac
import hashlib
import unittest
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select

from orchestrator.api.main import create_app
from orchestrator.api.routes_webhook import _build_command_followup_message, _run_discord_command_followup
from orchestrator.api.schemas import DiscordCommandResponse
from orchestrator.core.config import get_settings
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import Tenant


class JiraWebhookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.database_url = f"sqlite:///{self.temp_dir.name}/webhook_test.db"
        self.webhook_secret_env = "ORCHESTRATOR_TEST_WEBHOOK_SECRET"
        self.webhook_secret_value = "super-secret-token"
        self.github_webhook_secret_env = "ORCHESTRATOR_TEST_GITHUB_WEBHOOK_SECRET"
        self.github_webhook_secret_value = "github-super-secret-token"

        os.environ["ORCHESTRATOR_DATABASE_URL"] = self.database_url
        os.environ["ORCHESTRATOR_ADMIN_USERNAME"] = "admin"
        os.environ["ORCHESTRATOR_ADMIN_PASSWORD"] = "secret"
        os.environ["ORCHESTRATOR_SECRETS_ENCRYPTION_KEY"] = Fernet.generate_key().decode("utf-8")
        os.environ[self.webhook_secret_env] = self.webhook_secret_value
        os.environ[self.github_webhook_secret_env] = self.github_webhook_secret_value

        get_settings.cache_clear()
        reset_db_engine_cache()
        run_migrations(database_url=self.database_url)
        self.session_factory = create_session_factory(database_url=self.database_url)

        self.client = TestClient(create_app())
        self._create_tenant("tenant-webhook")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        os.environ.pop(self.webhook_secret_env, None)
        os.environ.pop(self.github_webhook_secret_env, None)
        os.environ.pop("ORCHESTRATOR_GITHUB_WEBHOOK_SECRET_REF", None)
        os.environ.pop("ORCHESTRATOR_WEBHOOK_MAX_BODY_BYTES", None)
        os.environ.pop("ORCHESTRATOR_SECRETS_ENCRYPTION_KEY", None)
        get_settings.cache_clear()
        reset_db_engine_cache()

    def _create_tenant(
        self,
        tenant_id: str,
        webhook_secret_ref: str | None = None,
        github_webhook_secret_ref: str | None = None,
        github_installation_id: str = "12345",
        is_enabled: bool = True,
        max_concurrent_runs: int = 2,
    ) -> None:
        payload = {
            "name": tenant_id,
            "is_enabled": is_enabled,
            "jira": {
                "mcp_endpoint": "https://mcp.example.test",
                "project_keys": ["TP"],
                "ready_statuses": ["Ready for Agent"],
                "ready_jql": 'project = TP AND status = "Ready for Agent"',
                "ready_label": "agent:ready",
                "in_progress_label": "agent:in-progress",
                "blocked_label": "agent:blocked",
                "done_label": None,
                "webhook_secret_ref": webhook_secret_ref,
            },
            "github": {
                "mode": "github_app",
                "webhook_secret_ref": github_webhook_secret_ref,
                "installation_id": github_installation_id,
            },
            "repos": {
                "allowlist": ["https://github.com/example/repo"],
                "mapping_rules_by_project_key": {"TP": "https://github.com/example/repo"},
                "mapping_rules_by_component": {},
                "fallback_repo": None,
            },
            "policy": {
                "allow_jira_transitions": False,
                "allow_pr_creation": True,
                "allow_label_mutations": True,
                "max_runtime_minutes": 30,
                "max_dev_test_review_loops": 2,
                "max_concurrent_runs": max_concurrent_runs,
                "allowed_commands": [],
                "require_agents_md": False,
            },
            "discord": None,
        }
        response = self.client.post("/api/admin/tenants", json=payload, auth=("admin", "secret"))
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["tenant_id"], tenant_id)

    def _sign_github_payload(self, payload_bytes: bytes, secret: str) -> str:
        digest = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
        return f"sha256={digest}"

    def _jira_issue_payload(
        self,
        *,
        issue_key: str,
        labels: list[str] | None = None,
        status_name: str = "Ready for Agent",
        status_category_key: str = "indeterminate",
    ) -> dict:
        return {
            "issue": {
                "key": issue_key,
                "fields": {
                    "labels": labels or [],
                    "status": {
                        "name": status_name,
                        "statusCategory": {"key": status_category_key},
                    },
                },
            }
        }

    def test_webhook_ignores_disabled_tenant(self) -> None:
        self._create_tenant("tenant-disabled", is_enabled=False)
        payload = self._jira_issue_payload(issue_key="TP-126", labels=["agent:ready"])

        response = self.client.post("/jira/webhook/tenant-disabled", json=payload)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["enqueued"])
        self.assertEqual(response.json()["reason"], "tenant_disabled")

    def test_webhook_requires_ready_status(self) -> None:
        payload = self._jira_issue_payload(issue_key="TP-123", status_name="To Do", labels=["agent:ready"])

        response = self.client.post("/jira/webhook/tenant-webhook", json=payload)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["enqueued"])
        self.assertEqual(response.json()["reason"], "status_not_ready")
        self.assertIn("Move the issue to a ready status", response.json()["guidance"])

    def test_webhook_ignores_done_issue_status(self) -> None:
        payload = self._jira_issue_payload(
            issue_key="TP-123",
            status_name="Done",
            status_category_key="done",
            labels=["agent:ready"],
        )

        response = self.client.post("/jira/webhook/tenant-webhook", json=payload)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["enqueued"])
        self.assertEqual(response.json()["reason"], "issue_done")

    def test_webhook_enqueues_once_for_active_issue(self) -> None:
        payload = self._jira_issue_payload(issue_key="TP-124", labels=["agent:ready"])

        first = self.client.post("/jira/webhook/tenant-webhook", json=payload)
        second = self.client.post("/jira/webhook/tenant-webhook", json=payload)

        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.json()["enqueued"])
        self.assertEqual(first.json()["trigger_reason"], "ready_status_recheck")

        self.assertEqual(second.status_code, 200)
        self.assertFalse(second.json()["enqueued"])
        self.assertEqual(second.json()["reason"], "run_already_active")
        self.assertEqual(second.json()["run_id"], first.json()["run_id"])
        self.assertEqual(second.json()["trigger_reason"], "ready_status_recheck")

    def test_webhook_records_last_delivery_metadata(self) -> None:
        payload = self._jira_issue_payload(issue_key="TP-777", labels=["agent:ready"])
        headers = {"X-Atlassian-Webhook-Identifier": "delivery-meta-1"}

        response = self.client.post("/jira/webhook/tenant-webhook", json=payload, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["enqueued"])

        tenant_response = self.client.get("/api/admin/tenants/tenant-webhook", auth=("admin", "secret"))
        self.assertEqual(tenant_response.status_code, 200)
        jira_config = tenant_response.json()["jira"]
        self.assertEqual(jira_config["webhook_last_delivery_id"], "delivery-meta-1")
        self.assertEqual(jira_config["webhook_last_issue_key"], "TP-777")
        self.assertIsNotNone(jira_config["webhook_last_received_at"])

    def test_webhook_marks_transition_into_ready_status(self) -> None:
        payload = self._jira_issue_payload(issue_key="TP-128", labels=["agent:ready"])
        payload["changelog"] = {
            "items": [
                {
                    "field": "status",
                    "fromString": "To Do",
                    "toString": "Ready for Agent",
                }
            ]
        }

        response = self.client.post("/jira/webhook/tenant-webhook", json=payload)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["enqueued"])
        self.assertEqual(response.json()["trigger_reason"], "status_transition_to_ready")

    def test_webhook_deduplicates_delivery_identifier(self) -> None:
        payload = self._jira_issue_payload(issue_key="TP-126", labels=["agent:ready"])
        headers = {"X-Atlassian-Webhook-Identifier": "delivery-123"}

        first = self.client.post("/jira/webhook/tenant-webhook", json=payload, headers=headers)
        second = self.client.post("/jira/webhook/tenant-webhook", json=payload, headers=headers)

        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.json()["enqueued"])
        self.assertEqual(second.status_code, 200)
        self.assertFalse(second.json()["enqueued"])
        self.assertEqual(second.json()["reason"], "duplicate_delivery")
        self.assertEqual(second.json()["run_id"], first.json()["run_id"])

    def test_webhook_respects_tenant_concurrency_limit(self) -> None:
        self._create_tenant("tenant-single", max_concurrent_runs=1)
        first_payload = self._jira_issue_payload(issue_key="TP-126", labels=["agent:ready"])
        second_payload = self._jira_issue_payload(issue_key="TP-127", labels=["agent:ready"])

        first = self.client.post("/jira/webhook/tenant-single", json=first_payload)
        second = self.client.post("/jira/webhook/tenant-single", json=second_payload)

        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.json()["enqueued"])
        self.assertEqual(second.status_code, 200)
        self.assertFalse(second.json()["enqueued"])
        self.assertEqual(second.json()["reason"], "tenant_concurrency_limit_reached")

    def test_webhook_unknown_tenant_returns_404(self) -> None:
        payload = self._jira_issue_payload(issue_key="TP-999", labels=["agent:ready"])

        response = self.client.post("/jira/webhook/missing-tenant", json=payload)

        self.assertEqual(response.status_code, 404)

    def test_webhook_requires_valid_token_when_secret_ref_configured(self) -> None:
        self._create_tenant("tenant-auth", webhook_secret_ref=self.webhook_secret_env)
        payload = self._jira_issue_payload(issue_key="TP-125", labels=["agent:ready"])

        unauthenticated = self.client.post("/jira/webhook/tenant-auth", json=payload)
        invalid_token = self.client.post(
            "/jira/webhook/tenant-auth",
            json=payload,
            headers={"X-Webhook-Token": "incorrect"},
        )
        valid_token = self.client.post(
            "/jira/webhook/tenant-auth",
            json=payload,
            headers={"X-Webhook-Token": self.webhook_secret_value},
        )

        self.assertEqual(unauthenticated.status_code, 401)
        self.assertEqual(invalid_token.status_code, 401)
        self.assertEqual(valid_token.status_code, 200)
        self.assertTrue(valid_token.json()["enqueued"])

    def test_github_webhook_ping_is_accepted(self) -> None:
        response = self.client.post(
            "/github/webhook",
            json={"zen": "keep it logically awesome"},
            headers={
                "X-GitHub-Event": "ping",
                "X-GitHub-Delivery": "gh-delivery-1",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["accepted"])
        self.assertEqual(response.json()["reason"], "ping")

    def test_github_webhook_unknown_installation_is_accepted_without_handler(self) -> None:
        response = self.client.post(
            "/github/webhook",
            json={
                "action": "opened",
                "installation": {"id": 999999},
                "repository": {"full_name": "example/repo"},
            },
            headers={
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "gh-delivery-2",
            },
        )

        self.assertEqual(response.status_code, 202)
        self.assertFalse(response.json()["accepted"])
        self.assertEqual(response.json()["reason"], "unknown_installation")

    def test_github_webhook_resolves_tenant_from_installation_id(self) -> None:
        response = self.client.post(
            "/github/webhook",
            json={
                "action": "synchronize",
                "installation": {"id": 12345},
                "repository": {"full_name": "example/repo"},
            },
            headers={
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "gh-delivery-3",
            },
        )

        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.json()["accepted"])
        self.assertEqual(response.json()["tenant_id"], "tenant-webhook")
        self.assertEqual(response.json()["reason"], "accepted_no_handler")

    def test_github_webhook_rejects_invalid_signature_when_global_secret_configured(self) -> None:
        os.environ["ORCHESTRATOR_GITHUB_WEBHOOK_SECRET_REF"] = self.github_webhook_secret_env
        payload = {
            "action": "opened",
            "installation": {"id": 12345},
            "repository": {"full_name": "example/repo"},
        }
        payload_bytes = json.dumps(payload).encode("utf-8")

        response = self.client.post(
            "/github/webhook",
            content=payload_bytes,
            headers={
                "content-type": "application/json",
                "X-GitHub-Event": "pull_request",
                "X-Hub-Signature-256": self._sign_github_payload(payload_bytes, "wrong-secret"),
            },
        )

        self.assertEqual(response.status_code, 401)

    def test_github_webhook_accepts_valid_signature_when_global_secret_configured(self) -> None:
        os.environ["ORCHESTRATOR_GITHUB_WEBHOOK_SECRET_REF"] = self.github_webhook_secret_env
        payload = {
            "action": "opened",
            "installation": {"id": 12345},
            "repository": {"full_name": "example/repo"},
        }
        payload_bytes = json.dumps(payload).encode("utf-8")
        signature = self._sign_github_payload(payload_bytes, self.github_webhook_secret_value)

        response = self.client.post(
            "/github/webhook",
            content=payload_bytes,
            headers={
                "content-type": "application/json",
                "X-GitHub-Event": "pull_request",
                "X-Hub-Signature-256": signature,
            },
        )

        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.json()["accepted"])
        self.assertEqual(response.json()["reason"], "accepted_no_handler")

    def test_github_webhook_enforces_payload_size_limit(self) -> None:
        os.environ["ORCHESTRATOR_WEBHOOK_MAX_BODY_BYTES"] = "20"
        payload = {"zen": "abcdefghijklmnopqrstuvwxyz"}
        payload_bytes = json.dumps(payload).encode("utf-8")

        response = self.client.post(
            "/github/webhook",
            content=payload_bytes,
            headers={
                "content-type": "application/json",
                "X-GitHub-Event": "ping",
            },
        )

        self.assertEqual(response.status_code, 413)

    def test_jira_webhook_uses_managed_secret_ref(self) -> None:
        managed_ref = "secret/jira-managed-token"
        self.client.put(
            "/api/admin/secrets/secret%2Fjira-managed-token",
            json={"value": "managed-jira-token"},
            auth=("admin", "secret"),
        )
        self._create_tenant("tenant-managed-jira", webhook_secret_ref=managed_ref)

        payload = self._jira_issue_payload(issue_key="TP-555", labels=["agent:ready"])
        response = self.client.post(
            "/jira/webhook/tenant-managed-jira",
            json=payload,
            headers={"X-Webhook-Token": "managed-jira-token"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["enqueued"])

    def test_github_webhook_uses_managed_global_secret_ref(self) -> None:
        os.environ["ORCHESTRATOR_GITHUB_WEBHOOK_SECRET_REF"] = "secret/github-global-webhook"
        self.client.put(
            "/api/admin/secrets/secret%2Fgithub-global-webhook",
            json={"value": "managed-global-secret"},
            auth=("admin", "secret"),
        )
        payload = {
            "action": "opened",
            "installation": {"id": 12345},
            "repository": {"full_name": "example/repo"},
        }
        payload_bytes = json.dumps(payload).encode("utf-8")
        signature = self._sign_github_payload(payload_bytes, "managed-global-secret")

        response = self.client.post(
            "/github/webhook",
            content=payload_bytes,
            headers={
                "content-type": "application/json",
                "X-GitHub-Event": "pull_request",
                "X-Hub-Signature-256": signature,
            },
        )

        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.json()["accepted"])

    def test_discord_interaction_commands_are_deferred_and_processed_async(self) -> None:
        payload = {
            "type": 2,
            "application_id": "discord-app-1",
            "token": "interaction-token-1",
            "channel_id": "discord-channel-1",
            "data": {"name": "help"},
            "member": {"user": {"id": "discord-user-1"}},
        }
        fake_tenant = SimpleNamespace(tenant_id="tenant-webhook")

        def _capture_and_close(coro):
            coro.close()
            return MagicMock(name="discord-task")

        with (
            patch("orchestrator.api.routes_webhook._resolve_discord_interactions_public_key", return_value=b"\x01" * 32),
            patch("orchestrator.api.routes_webhook._validate_discord_interaction_signature"),
            patch("orchestrator.api.routes_webhook._find_tenant_for_discord_channel", return_value=fake_tenant),
            patch("orchestrator.api.routes_webhook.asyncio.create_task", side_effect=_capture_and_close) as create_task_mock,
        ):
            response = self.client.post("/discord/interactions", json=payload)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["type"], 5)
        self.assertEqual(body["data"]["flags"], 64)
        create_task_mock.assert_called_once()

    def test_discord_component_interactions_are_deferred_and_processed_async(self) -> None:
        payload = {
            "type": 3,
            "application_id": "discord-app-1",
            "token": "interaction-token-1",
            "channel_id": "discord-channel-1",
            "data": {"custom_id": "ask.approve.0123456789abcdef0123456789abcdef"},
            "member": {"user": {"id": "discord-user-1"}},
        }
        fake_tenant = SimpleNamespace(tenant_id="tenant-webhook")

        def _capture_and_close(coro):
            coro.close()
            return MagicMock(name="discord-component-task")

        with (
            patch("orchestrator.api.routes_webhook._resolve_discord_interactions_public_key", return_value=b"\x01" * 32),
            patch("orchestrator.api.routes_webhook._validate_discord_interaction_signature"),
            patch("orchestrator.api.routes_webhook._find_tenant_for_discord_channel", return_value=fake_tenant),
            patch("orchestrator.api.routes_webhook.asyncio.create_task", side_effect=_capture_and_close) as create_task_mock,
        ):
            response = self.client.post("/discord/interactions", json=payload)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["type"], 5)
        self.assertEqual(body["data"]["flags"], 64)
        create_task_mock.assert_called_once()

    def test_discord_reply_button_component_returns_modal(self) -> None:
        payload = {
            "type": 3,
            "application_id": "discord-app-1",
            "token": "interaction-token-1",
            "channel_id": "discord-channel-1",
            "data": {"custom_id": "ask.reply.open"},
            "message": {"id": "123456789012345678"},
            "member": {"user": {"id": "discord-user-1"}},
        }
        fake_tenant = SimpleNamespace(tenant_id="tenant-webhook")

        with (
            patch("orchestrator.api.routes_webhook._resolve_discord_interactions_public_key", return_value=b"\x01" * 32),
            patch("orchestrator.api.routes_webhook._validate_discord_interaction_signature"),
            patch("orchestrator.api.routes_webhook._find_tenant_for_discord_channel", return_value=fake_tenant),
        ):
            response = self.client.post("/discord/interactions", json=payload)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["type"], 9)
        self.assertEqual(body["data"]["custom_id"], "ask.reply.123456789012345678")
        self.assertEqual(body["data"]["components"][0]["components"][0]["custom_id"], "question")

    def test_discord_reply_message_command_returns_modal(self) -> None:
        payload = {
            "type": 2,
            "application_id": "discord-app-1",
            "token": "interaction-token-1",
            "channel_id": "discord-channel-1",
            "data": {
                "name": "reply",
                "type": 3,
                "target_id": "123456789012345678",
                "resolved": {
                    "messages": {
                        "123456789012345678": {
                            "id": "123456789012345678",
                            "author": {"id": "discord-app-1"},
                        }
                    }
                },
            },
            "member": {"user": {"id": "discord-user-1"}},
        }

        with (
            patch("orchestrator.api.routes_webhook._resolve_discord_interactions_public_key", return_value=b"\x01" * 32),
            patch("orchestrator.api.routes_webhook._validate_discord_interaction_signature"),
        ):
            response = self.client.post("/discord/interactions", json=payload)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["type"], 9)
        self.assertEqual(body["data"]["custom_id"], "ask.reply.123456789012345678")
        self.assertEqual(body["data"]["components"][0]["components"][0]["custom_id"], "question")

    def test_discord_reply_modal_submit_is_deferred_and_processed_async(self) -> None:
        payload = {
            "type": 5,
            "application_id": "discord-app-1",
            "token": "interaction-token-1",
            "channel_id": "discord-channel-1",
            "data": {
                "custom_id": "ask.reply.123456789012345678",
                "components": [
                    {
                        "type": 1,
                        "components": [
                            {
                                "type": 4,
                                "custom_id": "question",
                                "value": "What changed since the previous update?",
                            }
                        ],
                    }
                ],
            },
            "member": {"user": {"id": "discord-user-1"}},
        }
        fake_tenant = SimpleNamespace(tenant_id="tenant-webhook")
        run_followup_mock = MagicMock()

        with (
            patch("orchestrator.api.routes_webhook._resolve_discord_interactions_public_key", return_value=b"\x01" * 32),
            patch("orchestrator.api.routes_webhook._validate_discord_interaction_signature"),
            patch("orchestrator.api.routes_webhook._find_tenant_for_discord_channel", return_value=fake_tenant),
            patch("orchestrator.api.routes_webhook._run_discord_command_followup", run_followup_mock),
            patch("orchestrator.api.routes_webhook.asyncio.create_task", return_value=MagicMock()) as create_task_mock,
        ):
            response = self.client.post("/discord/interactions", json=payload)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["type"], 5)
        self.assertEqual(body["data"]["flags"], 64)
        create_task_mock.assert_called_once()
        run_followup_mock.assert_called_once_with(
            tenant_id="tenant-webhook",
            user_id="discord-user-1",
            channel_id="discord-channel-1",
            command_text="!ask What changed since the previous update?",
            application_id="discord-app-1",
            interaction_token="interaction-token-1",
            reply_to_message_id="123456789012345678",
        )

    def test_discord_followup_formats_issue_and_pr_references_as_hyperlinks(self) -> None:
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
                        "jira_url": "https://example.atlassian.net/browse/TP-999",
                        "pr_url": "https://github.com/example/repo/pull/77",
                    },
                ),
            )

        self.assertIn("[TP-999](https://example.atlassian.net/browse/TP-999)", message)
        self.assertIn("[Open PR](https://github.com/example/repo/pull/77)", message)

    def test_discord_reply_followup_posts_to_thread_without_webhook_followup(self) -> None:
        with (
            patch(
                "orchestrator.api.routes_webhook.execute_discord_command",
                return_value=DiscordCommandResponse(
                    ok=True,
                    command="ask",
                    message="Done.",
                    data={"issue_key": "TP-324"},
                ),
            ),
            patch("orchestrator.api.routes_webhook._send_discord_thread_followup") as thread_send_mock,
            patch("orchestrator.api.routes_webhook._send_discord_interaction_followup") as interaction_send_mock,
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

    def test_discord_ask_followup_creates_new_thread_for_initial_response(self) -> None:
        with (
            patch(
                "orchestrator.api.routes_webhook.execute_discord_command",
                return_value=DiscordCommandResponse(
                    ok=True,
                    command="ask",
                    message="Done.",
                    data={"issue_key": "TP-324"},
                ),
            ),
            patch("orchestrator.api.routes_webhook._send_discord_ask_response_with_thread") as ask_thread_send_mock,
            patch("orchestrator.api.routes_webhook._send_discord_thread_followup") as thread_send_mock,
            patch("orchestrator.api.routes_webhook._send_discord_interaction_followup") as interaction_send_mock,
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
        interaction_send_mock.assert_not_called()
