import os
import unittest
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from orchestrator.api.main import create_app
from orchestrator.core.config import get_settings
from orchestrator.core.secrets import encrypt_value
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import JiraOAuthConnection
from orchestrator.tools.github_app import InstallationRepository


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
        os.environ["ORCHESTRATOR_PUBLIC_API_BASE_URL"] = "http://localhost:4000"
        os.environ["ORCHESTRATOR_JIRA_OAUTH_STATE_SECRET"] = "jira-oauth-state-secret"
        os.environ["ORCHESTRATOR_JIRA_OAUTH_CLIENT_ID_REF"] = "secret/jira-client-id"
        os.environ["ORCHESTRATOR_JIRA_OAUTH_CLIENT_SECRET_REF"] = "secret/jira-client-secret"
        os.environ["ORCHESTRATOR_SECRETS_ENCRYPTION_KEY"] = Fernet.generate_key().decode("utf-8")

        os.environ["secret/app-id"] = "12345"
        os.environ["secret/private-key"] = "not-a-real-key-for-tests"
        os.environ["secret/jira-client-id"] = "jira-client-id"
        os.environ["secret/jira-client-secret"] = "jira-client-secret"

        get_settings.cache_clear()
        reset_db_engine_cache()
        run_migrations(database_url=self.database_url)

        self.client = TestClient(create_app())

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        os.environ.pop("secret/app-id", None)
        os.environ.pop("secret/private-key", None)
        os.environ.pop("secret/jira-client-id", None)
        os.environ.pop("secret/jira-client-secret", None)

        os.environ.pop("ORCHESTRATOR_GITHUB_APP_SLUG", None)
        os.environ.pop("ORCHESTRATOR_GITHUB_INSTALL_STATE_SECRET", None)
        os.environ.pop("ORCHESTRATOR_ADMIN_UI_BASE_URL", None)
        os.environ.pop("ORCHESTRATOR_PUBLIC_API_BASE_URL", None)
        os.environ.pop("ORCHESTRATOR_JIRA_OAUTH_STATE_SECRET", None)
        os.environ.pop("ORCHESTRATOR_JIRA_OAUTH_CLIENT_ID_REF", None)
        os.environ.pop("ORCHESTRATOR_JIRA_OAUTH_CLIENT_SECRET_REF", None)
        os.environ.pop("ORCHESTRATOR_SECRETS_ENCRYPTION_KEY", None)

        get_settings.cache_clear()
        reset_db_engine_cache()

    def _tenant_payload(self) -> dict:
        return {
            "name": "Tenant A",
            "is_enabled": True,
            "jira": {
                "connection_id": "conn-1",
                "project_keys": ["TP"],
                "ready_statuses": ["Ready for Agent"],
                "ready_jql": 'project = TP AND status = "Ready for Agent"',
                "ready_label": "agent:ready",
                "in_progress_label": "agent:in-progress",
                "blocked_label": "agent:blocked",
                "done_label": "agent:done",
                "webhook_secret_ref": "secret/webhook",
            },
            "github": {
                "mode": "github_app",
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

    def _insert_jira_connection(self, connection_id: str = "conn-1") -> None:
        session_factory = create_session_factory(self.database_url)
        settings = get_settings()
        now = datetime.now(timezone.utc)
        with session_factory() as session:
            session.add(
                JiraOAuthConnection(
                    connection_id=connection_id,
                    account_id="account-1",
                    account_email="test@example.com",
                    cloud_id="cloud-1",
                    site_url="https://example.atlassian.net",
                    scopes=["read:jira-work", "write:jira-work"],
                    access_token_encrypted=encrypt_value(
                        plaintext="access-token",
                        encryption_key=settings.secrets_encryption_key,
                    ),
                    refresh_token_encrypted=encrypt_value(
                        plaintext="refresh-token",
                        encryption_key=settings.secrets_encryption_key,
                    ),
                    access_token_expires_at=now + timedelta(hours=1),
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

    def test_admin_routes_require_auth(self) -> None:
        response = self.client.get("/api/admin/tenants")
        self.assertEqual(response.status_code, 401)

    def test_managed_secret_upsert_and_resolve(self) -> None:
        put_response = self.client.put(
            "/api/admin/secrets/secret%2Fgithub-webhook",
            json={"value": "managed-webhook-secret"},
            auth=("admin", "secret"),
        )
        self.assertEqual(put_response.status_code, 200)
        self.assertEqual(put_response.json()["secret_ref"], "secret/github-webhook")
        self.assertEqual(put_response.json()["source"], "managed")

        list_response = self.client.get("/api/admin/secrets", auth=("admin", "secret"))
        self.assertEqual(list_response.status_code, 200)
        refs = [item["secret_ref"] for item in list_response.json()]
        self.assertIn("secret/github-webhook", refs)

        resolve_response = self.client.post(
            "/api/admin/secrets/resolve",
            json={"secret_ref": "secret/github-webhook"},
            auth=("admin", "secret"),
        )
        self.assertEqual(resolve_response.status_code, 200)
        self.assertTrue(resolve_response.json()["resolved"])
        self.assertEqual(resolve_response.json()["source"], "managed")

    def test_jira_connect_uses_managed_secret_when_env_not_set(self) -> None:
        os.environ.pop("secret/jira-client-id", None)
        os.environ.pop("secret/jira-client-secret", None)

        self.client.put(
            "/api/admin/secrets/secret%2Fjira-client-id",
            json={"value": "jira-client-id-managed"},
            auth=("admin", "secret"),
        )
        self.client.put(
            "/api/admin/secrets/secret%2Fjira-client-secret",
            json={"value": "jira-client-secret-managed"},
            auth=("admin", "secret"),
        )

        response = self.client.post(
            "/api/admin/jira/connect/start?return_to=wizard",
            auth=("admin", "secret"),
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("jira-client-id-managed", response.json()["authorize_url"])

    def test_create_and_update_tenant(self) -> None:
        payload = self._tenant_payload()
        self._insert_jira_connection(connection_id="conn-1")

        create_response = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_response.status_code, 201)
        self.assertEqual(create_response.json()["tenant_id"], "tenant-a")
        self.assertEqual(create_response.json()["jira"]["ready_statuses"], ["Ready for Agent"])

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

        class _FakeJiraClient:
            def list_projects(self, *, access_token: str, cloud_id: str):  # noqa: ANN001
                return []

        with (
            patch("orchestrator.api.routes_admin._refresh_jira_connection_tokens", return_value="access-token"),
            patch("orchestrator.api.routes_admin._jira_oauth_client", return_value=_FakeJiraClient()),
        ):
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

        repo_bootstrap = self.client.get(
            "/api/admin/tenants/tenant-a/repo-bootstrap",
            auth=("admin", "secret"),
        )
        self.assertEqual(repo_bootstrap.status_code, 200)
        self.assertEqual(repo_bootstrap.json(), [])

    def test_ready_preview_returns_eligible_issues(self) -> None:
        payload = self._tenant_payload()
        self._insert_jira_connection(connection_id="conn-1")
        create_response = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_response.status_code, 201)

        class _FakeJiraClient:
            def search_issues_by_jql(  # noqa: ANN001
                self,
                *,
                access_token: str,
                cloud_id: str,
                jql: str,
                max_results: int = 20,
            ):
                self.last_jql = jql
                self.last_max_results = max_results
                self.last_access_token = access_token
                self.last_cloud_id = cloud_id
                return [
                    type("Issue", (), {"key": "TP-101", "summary": "Ready issue", "status": "Ready for Agent"})(),
                    type("Issue", (), {"key": "TP-102", "summary": "Another ready issue", "status": "Ready"})(),
                ]

        fake_client = _FakeJiraClient()
        with (
            patch("orchestrator.api.routes_admin._refresh_jira_connection_tokens", return_value="access-token"),
            patch("orchestrator.api.routes_admin._jira_oauth_client", return_value=fake_client),
        ):
            preview_response = self.client.get(
                "/api/admin/tenants/tenant-a/ready-preview",
                auth=("admin", "secret"),
            )

        self.assertEqual(preview_response.status_code, 200)
        body = preview_response.json()
        self.assertEqual(body["ready_statuses"], ["Ready for Agent"])
        self.assertIn("status", body["ready_jql"])
        self.assertEqual(len(body["eligible_issues"]), 2)
        self.assertEqual(body["eligible_issues"][0]["key"], "TP-101")
        self.assertIn("executable only", body["guidance"])
        self.assertEqual(fake_client.last_access_token, "access-token")

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

    def test_create_tenant_generates_unique_slug_id(self) -> None:
        payload = self._tenant_payload()
        first = self.client.post("/api/admin/tenants", json=payload, auth=("admin", "secret"))
        self.assertEqual(first.status_code, 201)
        self.assertEqual(first.json()["tenant_id"], "tenant-a")

        second = self.client.post("/api/admin/tenants", json=payload, auth=("admin", "secret"))
        self.assertEqual(second.status_code, 201)
        self.assertEqual(second.json()["tenant_id"], "tenant-a-2")

    def test_start_install_supports_wizard_redirect(self) -> None:
        payload = self._tenant_payload()
        payload["github"]["installation_id"] = None

        create_response = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_response.status_code, 201)

        start_response = self.client.post(
            "/api/admin/tenants/tenant-a/github/install/start?return_to=wizard",
            auth=("admin", "secret"),
        )
        self.assertEqual(start_response.status_code, 200)
        install_url = start_response.json()["install_url"]
        parsed = urlparse(install_url)
        state_token = parse_qs(parsed.query).get("state", [None])[0]
        self.assertIsNotNone(state_token)

        callback_response = self.client.get(
            "/api/admin/github/install/callback",
            params={
                "state": state_token,
                "installation_id": "11111",
            },
            follow_redirects=False,
        )
        self.assertEqual(callback_response.status_code, 302)
        self.assertIn("/tenants/new?tenant_id=tenant-a&github_install=success", callback_response.headers.get("location", ""))

    def test_list_github_repositories_for_tenant(self) -> None:
        payload = self._tenant_payload()
        payload["github"]["installation_id"] = "12345"
        create_response = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_response.status_code, 201)

        class _FakeClient:
            def list_installation_repositories(self):  # noqa: ANN001
                return [
                    InstallationRepository(
                        full_name="example/repo-one",
                        html_url="https://github.com/example/repo-one",
                        default_branch="main",
                        private=False,
                    ),
                    InstallationRepository(
                        full_name="example/repo-two",
                        html_url="https://github.com/example/repo-two",
                        default_branch="develop",
                        private=True,
                    ),
                ]

        with patch("orchestrator.api.routes_admin.github_client_from_tenant_config", return_value=_FakeClient()):
            response = self.client.get(
                "/api/admin/tenants/tenant-a/github/repositories",
                auth=("admin", "secret"),
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload), 2)
        self.assertEqual(payload[0]["full_name"], "example/repo-one")

    def test_jira_connect_start_requires_tenant_for_edit_mode(self) -> None:
        response = self.client.post(
            "/api/admin/jira/connect/start?return_to=edit",
            auth=("admin", "secret"),
        )
        self.assertEqual(response.status_code, 400)

    def test_jira_connect_wizard_callback_creates_connection(self) -> None:
        start_response = self.client.post(
            "/api/admin/jira/connect/start?return_to=wizard",
            auth=("admin", "secret"),
        )
        self.assertEqual(start_response.status_code, 200)
        authorize_url = start_response.json()["authorize_url"]
        parsed = urlparse(authorize_url)
        state_token = parse_qs(parsed.query).get("state", [None])[0]
        self.assertIsNotNone(state_token)

        class _FakeClient:
            def exchange_code(self, *, code: str):  # noqa: ANN001
                now = datetime.now(timezone.utc)
                return type(
                    "TokenSet",
                    (),
                    {
                        "access_token": "access-token",
                        "refresh_token": "refresh-token",
                        "expires_at": now + timedelta(hours=1),
                        "scopes": ["read:jira-work", "write:jira-work"],
                    },
                )()

            def list_accessible_resources(self, *, access_token: str):  # noqa: ANN001
                return [
                    type(
                        "Resource",
                        (),
                        {
                            "cloud_id": "cloud-1",
                            "site_url": "https://example.atlassian.net",
                            "name": "Example",
                        },
                    )()
                ]

        with patch("orchestrator.api.routes_admin._jira_oauth_client", return_value=_FakeClient()):
            callback_response = self.client.get(
                "/api/admin/jira/connect/callback",
                params={"code": "abc123", "state": state_token},
                follow_redirects=False,
            )

        self.assertEqual(callback_response.status_code, 302)
        self.assertIn("/tenants/new?jira_oauth=success&jira_connection_id=", callback_response.headers.get("location", ""))

    def test_jira_connect_edit_callback_updates_tenant(self) -> None:
        payload = self._tenant_payload()
        payload["jira"]["connection_id"] = None
        create_response = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_response.status_code, 201)

        start_response = self.client.post(
            "/api/admin/jira/connect/start?return_to=edit&tenant_id=tenant-a",
            auth=("admin", "secret"),
        )
        self.assertEqual(start_response.status_code, 200)
        authorize_url = start_response.json()["authorize_url"]
        parsed = urlparse(authorize_url)
        state_token = parse_qs(parsed.query).get("state", [None])[0]
        self.assertIsNotNone(state_token)

        class _FakeClient:
            def exchange_code(self, *, code: str):  # noqa: ANN001
                now = datetime.now(timezone.utc)
                return type(
                    "TokenSet",
                    (),
                    {
                        "access_token": "access-token",
                        "refresh_token": "refresh-token",
                        "expires_at": now + timedelta(hours=1),
                        "scopes": ["read:jira-work", "write:jira-work"],
                    },
                )()

            def list_accessible_resources(self, *, access_token: str):  # noqa: ANN001
                return [
                    type(
                        "Resource",
                        (),
                        {
                            "cloud_id": "cloud-1",
                            "site_url": "https://example.atlassian.net",
                            "name": "Example",
                        },
                    )()
                ]

        with patch("orchestrator.api.routes_admin._jira_oauth_client", return_value=_FakeClient()):
            callback_response = self.client.get(
                "/api/admin/jira/connect/callback",
                params={"code": "abc123", "state": state_token},
                follow_redirects=False,
            )

        self.assertEqual(callback_response.status_code, 302)
        self.assertIn(
            "/tenants/tenant-a/edit?jira_oauth=success&jira_connection_id=",
            callback_response.headers.get("location", ""),
        )

        tenant_response = self.client.get("/api/admin/tenants/tenant-a", auth=("admin", "secret"))
        self.assertEqual(tenant_response.status_code, 200)
        self.assertTrue(tenant_response.json()["jira"]["connection_id"])


if __name__ == "__main__":
    unittest.main()
