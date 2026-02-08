import os
import json
import hmac
import hashlib
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from orchestrator.api.main import create_app
from orchestrator.core.config import get_settings
from orchestrator.core.discord_notifications import DiscordSendResult
from orchestrator.storage.db import reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.tools.github_app import PullRequestDetails, PullRequestFileChange, WorkflowCheckSuite


class JiraWebhookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.database_url = f"sqlite:///{self.temp_dir.name}/webhook_test.db"
        self.webhook_secret_env = "ORCHESTRATOR_TEST_WEBHOOK_SECRET"
        self.webhook_secret_value = "super-secret-token"
        self.github_webhook_secret_value = "github-super-secret-token"

        os.environ["ORCHESTRATOR_DATABASE_URL"] = self.database_url
        os.environ["ORCHESTRATOR_ADMIN_USERNAME"] = "admin"
        os.environ["ORCHESTRATOR_ADMIN_PASSWORD"] = "secret"
        os.environ["ORCHESTRATOR_SECRETS_ENCRYPTION_KEY"] = Fernet.generate_key().decode("utf-8")
        os.environ[self.webhook_secret_env] = self.webhook_secret_value

        get_settings.cache_clear()
        reset_db_engine_cache()
        run_migrations(database_url=self.database_url)

        self.client = TestClient(create_app())
        self._create_tenant("tenant-webhook")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        os.environ.pop(self.webhook_secret_env, None)
        os.environ.pop("GITHUB_WEBHOOK_SECRET", None)
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
        discord_config: dict | None = None,
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
                "github_repository": "https://github.com/example/repo",
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
            "discord": discord_config,
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
        self.assertFalse(response.json()["accepted"])
        self.assertEqual(response.json()["tenant_id"], "tenant-webhook")
        self.assertEqual(response.json()["reason"], "missing_pr_context")

    def test_github_webhook_rejects_invalid_signature_when_global_secret_configured(self) -> None:
        os.environ["GITHUB_WEBHOOK_SECRET"] = self.github_webhook_secret_value
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
        os.environ["GITHUB_WEBHOOK_SECRET"] = self.github_webhook_secret_value
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
        self.assertFalse(response.json()["accepted"])
        self.assertEqual(response.json()["reason"], "missing_pr_context")

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
        os.environ.pop("GITHUB_WEBHOOK_SECRET", None)
        self.client.put(
            "/api/admin/secrets/GITHUB_WEBHOOK_SECRET",
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
        self.assertFalse(response.json()["accepted"])
        self.assertEqual(response.json()["reason"], "missing_pr_context")

    def test_discord_webhook_executes_help_command(self) -> None:
        self._create_tenant(
            "tenant-discord",
            discord_config={
                "channel_id": "discord-channel-1",
                "notify_events": [],
                "allowed_user_ids": [],
                "command_secret_ref": None,
            },
        )
        response = self.client.post(
            "/discord/webhook/tenant-discord",
            json={"user_id": "u-1", "channel_id": "discord-channel-1", "command": "!help"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["accepted"])
        self.assertEqual(response.json()["result"]["command"], "help")

    def test_discord_webhook_requires_valid_command_token(self) -> None:
        self._create_tenant(
            "tenant-discord-auth",
            discord_config={
                "channel_id": "discord-channel-1",
                "notify_events": [],
                "allowed_user_ids": [],
                "command_secret_ref": self.webhook_secret_env,
            },
        )
        unauthenticated = self.client.post(
            "/discord/webhook/tenant-discord-auth",
            json={"user_id": "u-1", "channel_id": "discord-channel-1", "command": "!help"},
        )
        authenticated = self.client.post(
            "/discord/webhook/tenant-discord-auth",
            json={"user_id": "u-1", "channel_id": "discord-channel-1", "command": "!help"},
            headers={"X-Webhook-Token": self.webhook_secret_value},
        )
        self.assertEqual(unauthenticated.status_code, 401)
        self.assertEqual(authenticated.status_code, 200)

    def test_github_webhook_processes_review_and_posts_signal(self) -> None:
        class _FakeGitHubClient:
            def get_pull_request_details(self, *, repo_full_name: str, pr_number: int):  # noqa: ANN001
                return PullRequestDetails(
                    number=pr_number,
                    html_url=f"https://github.com/{repo_full_name}/pull/{pr_number}",
                    head_sha="abc123",
                )

            def list_check_suites(self, *, repo_full_name: str, ref: str):  # noqa: ANN001
                return [
                    WorkflowCheckSuite(name="CI", status="completed", conclusion="success"),
                    WorkflowCheckSuite(name="Security", status="completed", conclusion="success"),
                ]

            def list_pull_request_files(self, *, repo_full_name: str, pr_number: int):  # noqa: ANN001
                return [PullRequestFileChange(filename="orchestrator/api/routes_webhook.py", patch="+ok")]

        with (
            patch(
                "orchestrator.api.routes_webhook.github_client_from_tenant_config",
                return_value=_FakeGitHubClient(),
            ),
            patch(
                "orchestrator.api.routes_webhook.send_tenant_discord_message",
                return_value=DiscordSendResult(sent=True, reason="sent", channel_id="discord-channel-1"),
            ),
        ):
            response = self.client.post(
                "/github/webhook",
                json={
                    "action": "opened",
                    "installation": {"id": 12345},
                    "repository": {"full_name": "example/repo"},
                    "pull_request": {"number": 44, "body": "Summary present"},
                },
                headers={
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "gh-delivery-99",
                },
            )

        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.json()["accepted"])
        self.assertEqual(response.json()["reason"], "review_processed")
        self.assertEqual(response.json()["signals"][0]["state"], "ready")
