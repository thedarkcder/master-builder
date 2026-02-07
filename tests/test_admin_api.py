import os
import unittest
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from orchestrator.api.main import create_app
from orchestrator.core.config import get_settings
from orchestrator.storage.db import reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations


class AdminApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.database_url = f"sqlite:///{self.temp_dir.name}/admin_test.db"

        os.environ["ORCHESTRATOR_DATABASE_URL"] = self.database_url
        os.environ["ORCHESTRATOR_ADMIN_USERNAME"] = "admin"
        os.environ["ORCHESTRATOR_ADMIN_PASSWORD"] = "secret"
        os.environ["ORCHESTRATOR_GITHUB_APP_SLUG"] = "master-builder-app"
        os.environ["ORCHESTRATOR_GITHUB_INSTALL_STATE_SECRET"] = "unit-test-secret"
        os.environ["ORCHESTRATOR_ADMIN_UI_BASE_URL"] = "http://localhost:4100"
        os.environ["secret/app-id"] = "12345"
        os.environ["secret/private-key"] = "not-a-real-key-for-tests"

        get_settings.cache_clear()
        reset_db_engine_cache()
        run_migrations(database_url=self.database_url)

        self.client = TestClient(create_app())

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        os.environ.pop("secret/app-id", None)
        os.environ.pop("secret/private-key", None)
        os.environ.pop("ORCHESTRATOR_GITHUB_APP_SLUG", None)
        os.environ.pop("ORCHESTRATOR_GITHUB_INSTALL_STATE_SECRET", None)
        os.environ.pop("ORCHESTRATOR_ADMIN_UI_BASE_URL", None)
        get_settings.cache_clear()
        reset_db_engine_cache()

    def _tenant_payload(self) -> dict:
        return {
            "tenant_id": "tenant-a",
            "name": "Tenant A",
            "is_enabled": True,
            "jira": {
                "mcp_endpoint": "https://mcp.example.test",
                "auth_ref": "secret/jira",
                "project_keys": ["TP"],
                "ready_label": "agent:ready",
                "in_progress_label": "agent:in-progress",
                "blocked_label": "agent:blocked",
                "done_label": "agent:done",
                "ready_jql": "project = TP",
                "webhook_secret_ref": "secret/webhook",
            },
            "github": {
                "mode": "github_app",
                "app_id_ref": "secret/app-id",
                "private_key_ref": "secret/private-key",
                "webhook_secret_ref": "secret/github-webhook",
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
                "max_concurrent_runs": 2,
                "allowed_commands": ["python -m unittest"],
                "require_agents_md": False,
            },
            "discord": {
                "channel_id": None,
                "channel_name_template": "proj-{tenant_id}",
                "notify_events": ["run_started"],
            },
        }

    def test_admin_routes_require_auth(self) -> None:
        response = self.client.get("/api/admin/tenants")
        self.assertEqual(response.status_code, 401)

    def test_create_and_update_tenant(self) -> None:
        payload = self._tenant_payload()

        create_response = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_response.status_code, 201)
        self.assertEqual(create_response.json()["tenant_id"], "tenant-a")

        list_response = self.client.get("/api/admin/tenants", auth=("admin", "secret"))
        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(len(list_response.json()), 1)

        payload["name"] = "Tenant A Updated"
        payload["is_enabled"] = False

        update_response = self.client.put(
            "/api/admin/tenants/tenant-a",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(update_response.status_code, 200)
        self.assertEqual(update_response.json()["name"], "Tenant A Updated")
        self.assertFalse(update_response.json()["is_enabled"])

        jira_test = self.client.post(
            "/api/admin/tenants/tenant-a/test-jira",
            auth=("admin", "secret"),
        )
        self.assertEqual(jira_test.status_code, 200)
        self.assertTrue(jira_test.json()["ok"])

        github_test = self.client.post(
            "/api/admin/tenants/tenant-a/test-github",
            auth=("admin", "secret"),
        )
        self.assertEqual(github_test.status_code, 200)
        self.assertTrue(github_test.json()["ok"])

    def test_delete_tenant(self) -> None:
        payload = self._tenant_payload()
        create_response = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_response.status_code, 201)

        delete_response = self.client.delete("/api/admin/tenants/tenant-a", auth=("admin", "secret"))
        self.assertEqual(delete_response.status_code, 204)

        get_response = self.client.get("/api/admin/tenants/tenant-a", auth=("admin", "secret"))
        self.assertEqual(get_response.status_code, 404)

        list_response = self.client.get("/api/admin/tenants", auth=("admin", "secret"))
        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(len(list_response.json()), 0)

    def test_create_tenant_validates_required_project_keys(self) -> None:
        payload = self._tenant_payload()
        payload["jira"]["project_keys"] = []

        response = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(response.status_code, 422)

    def test_start_install_and_callback_persist_installation_id(self) -> None:
        payload = self._tenant_payload()
        payload["github"]["installation_id"] = None

        create_response = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_response.status_code, 201)

        start_response = self.client.post(
            "/api/admin/tenants/tenant-a/github/install/start",
            auth=("admin", "secret"),
        )
        self.assertEqual(start_response.status_code, 200)
        install_url = start_response.json()["install_url"]
        self.assertIn("https://github.com/apps/master-builder-app/installations/new", install_url)

        parsed = urlparse(install_url)
        state_token = parse_qs(parsed.query).get("state", [None])[0]
        self.assertIsNotNone(state_token)

        callback_response = self.client.get(
            "/api/admin/github/install/callback",
            params={
                "state": state_token,
                "installation_id": "98765",
                "setup_action": "install",
            },
            follow_redirects=False,
        )
        self.assertEqual(callback_response.status_code, 302)
        self.assertIn("/tenants/tenant-a/edit?github_install=success", callback_response.headers.get("location", ""))

        tenant_response = self.client.get("/api/admin/tenants/tenant-a", auth=("admin", "secret"))
        self.assertEqual(tenant_response.status_code, 200)
        self.assertEqual(tenant_response.json()["github"]["installation_id"], "98765")
