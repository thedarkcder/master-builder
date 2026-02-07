import os
import unittest
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from orchestrator.api.main import create_app
from orchestrator.core.config import get_settings
from orchestrator.storage.db import reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations


class JiraWebhookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.database_url = f"sqlite:///{self.temp_dir.name}/webhook_test.db"
        self.webhook_secret_env = "ORCHESTRATOR_TEST_WEBHOOK_SECRET"
        self.webhook_secret_value = "super-secret-token"

        os.environ["ORCHESTRATOR_DATABASE_URL"] = self.database_url
        os.environ["ORCHESTRATOR_ADMIN_USERNAME"] = "admin"
        os.environ["ORCHESTRATOR_ADMIN_PASSWORD"] = "secret"
        os.environ[self.webhook_secret_env] = self.webhook_secret_value

        get_settings.cache_clear()
        reset_db_engine_cache()
        run_migrations(database_url=self.database_url)

        self.client = TestClient(create_app())
        self._create_tenant("tenant-webhook")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        os.environ.pop(self.webhook_secret_env, None)
        get_settings.cache_clear()
        reset_db_engine_cache()

    def _create_tenant(
        self,
        tenant_id: str,
        webhook_secret_ref: str | None = None,
        is_enabled: bool = True,
        max_concurrent_runs: int = 2,
    ) -> None:
        payload = {
            "name": tenant_id,
            "is_enabled": is_enabled,
            "jira": {
                "mcp_endpoint": "https://mcp.example.test",
                "project_keys": ["TP"],
                "ready_label": "agent:ready",
                "in_progress_label": "agent:in-progress",
                "blocked_label": "agent:blocked",
                "done_label": None,
                "webhook_secret_ref": webhook_secret_ref,
            },
            "github": {
                "mode": "github_app",
                "app_id_ref": "secret/app-id",
                "private_key_ref": "secret/private-key",
                "webhook_secret_ref": None,
                "installation_id": "12345",
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

    def test_webhook_ignores_disabled_tenant(self) -> None:
        self._create_tenant("tenant-disabled", is_enabled=False)
        payload = {
            "issue": {
                "key": "TP-126",
                "fields": {"labels": ["agent:ready"]},
            }
        }

        response = self.client.post("/jira/webhook/tenant-disabled", json=payload)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["enqueued"])
        self.assertEqual(response.json()["reason"], "tenant_disabled")

    def test_webhook_requires_ready_label(self) -> None:
        payload = {
            "issue": {
                "key": "TP-123",
                "fields": {"labels": ["not-ready"]},
            }
        }

        response = self.client.post("/jira/webhook/tenant-webhook", json=payload)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["enqueued"])
        self.assertEqual(response.json()["reason"], "ready_label_missing")

    def test_webhook_enqueues_once_for_active_issue(self) -> None:
        payload = {
            "issue": {
                "key": "TP-124",
                "fields": {"labels": ["agent:ready"]},
            }
        }

        first = self.client.post("/jira/webhook/tenant-webhook", json=payload)
        second = self.client.post("/jira/webhook/tenant-webhook", json=payload)

        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.json()["enqueued"])

        self.assertEqual(second.status_code, 200)
        self.assertFalse(second.json()["enqueued"])
        self.assertEqual(second.json()["reason"], "run_already_active")
        self.assertEqual(second.json()["run_id"], first.json()["run_id"])

    def test_webhook_deduplicates_delivery_identifier(self) -> None:
        payload = {
            "issue": {
                "key": "TP-126",
                "fields": {"labels": ["agent:ready"]},
            }
        }
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
        first_payload = {
            "issue": {
                "key": "TP-126",
                "fields": {"labels": ["agent:ready"]},
            }
        }
        second_payload = {
            "issue": {
                "key": "TP-127",
                "fields": {"labels": ["agent:ready"]},
            }
        }

        first = self.client.post("/jira/webhook/tenant-single", json=first_payload)
        second = self.client.post("/jira/webhook/tenant-single", json=second_payload)

        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.json()["enqueued"])
        self.assertEqual(second.status_code, 200)
        self.assertFalse(second.json()["enqueued"])
        self.assertEqual(second.json()["reason"], "tenant_concurrency_limit_reached")

    def test_webhook_unknown_tenant_returns_404(self) -> None:
        payload = {
            "issue": {
                "key": "TP-999",
                "fields": {"labels": ["agent:ready"]},
            }
        }

        response = self.client.post("/jira/webhook/missing-tenant", json=payload)

        self.assertEqual(response.status_code, 404)

    def test_webhook_requires_valid_token_when_secret_ref_configured(self) -> None:
        self._create_tenant("tenant-auth", webhook_secret_ref=self.webhook_secret_env)
        payload = {
            "issue": {
                "key": "TP-125",
                "fields": {"labels": ["agent:ready"]},
            }
        }

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
