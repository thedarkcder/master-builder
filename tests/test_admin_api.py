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
from orchestrator.core.enforcement_context import EnforcementAssetsError
from orchestrator.core.secrets import encrypt_value
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import JiraOAuthConnection, Tenant
from orchestrator.tools.github_app import InstallationRepository


class AdminApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.database_url = f"sqlite:///{self.temp_dir.name}/admin_test.db"

        os.environ["ORCHESTRATOR_DATABASE_URL"] = self.database_url
        os.environ["ORCHESTRATOR_ADMIN_USERNAME"] = "admin"
        os.environ["ORCHESTRATOR_ADMIN_PASSWORD"] = "secret"
        os.environ["ORCHESTRATOR_GITHUB_INSTALL_STATE_SECRET"] = "unit-test-secret"
        os.environ["ORCHESTRATOR_ADMIN_UI_BASE_URL"] = "http://localhost:4100"
        os.environ["ORCHESTRATOR_PUBLIC_API_BASE_URL"] = "http://localhost:4000"
        os.environ["ORCHESTRATOR_JIRA_OAUTH_STATE_SECRET"] = "jira-oauth-state-secret"
        os.environ["ORCHESTRATOR_SECRETS_ENCRYPTION_KEY"] = Fernet.generate_key().decode("utf-8")

        os.environ["GITHUB_APP_ID"] = "12345"
        os.environ["GITHUB_CLIENT_SECRET"] = "not-a-real-key-for-tests"
        os.environ["JIRA_OAUTH_CLIENT_ID"] = "jira-client-id"
        os.environ["JIRA_OAUTH_CLIENT_SECRET"] = "jira-client-secret"

        get_settings.cache_clear()
        reset_db_engine_cache()
        run_migrations(database_url=self.database_url)

        self.client = TestClient(create_app())
        seed_slug_secret_response = self.client.put(
            "/api/admin/secrets/GITHUB_APP_SLUG",
            json={"value": "master-builder-app"},
            auth=("admin", "secret"),
        )
        if seed_slug_secret_response.status_code != 200:
            raise RuntimeError(
                f"Failed to seed GITHUB_APP_SLUG secret for tests: {seed_slug_secret_response.text}"
            )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        os.environ.pop("GITHUB_APP_ID", None)
        os.environ.pop("GITHUB_CLIENT_SECRET", None)
        os.environ.pop("JIRA_OAUTH_CLIENT_ID", None)
        os.environ.pop("JIRA_OAUTH_CLIENT_SECRET", None)

        os.environ.pop("ORCHESTRATOR_GITHUB_INSTALL_STATE_SECRET", None)
        os.environ.pop("ORCHESTRATOR_ADMIN_UI_BASE_URL", None)
        os.environ.pop("ORCHESTRATOR_PUBLIC_API_BASE_URL", None)
        os.environ.pop("ORCHESTRATOR_JIRA_OAUTH_STATE_SECRET", None)
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
                "github_repository": "https://github.com/example/repo",
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
                "channel_id": "discord-channel-1",
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

    def test_admin_login_issues_bearer_token(self) -> None:
        login_response = self.client.post(
            "/api/admin/auth/login",
            json={"username": "admin", "password": "secret"},
        )
        self.assertEqual(login_response.status_code, 200)
        token = login_response.json()["access_token"]
        self.assertTrue(token)
        self.assertEqual(login_response.json()["token_type"], "bearer")

        me_response = self.client.get(
            "/api/admin/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(me_response.status_code, 200)
        self.assertEqual(me_response.json()["username"], "admin")

        tenants_response = self.client.get(
            "/api/admin/tenants",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(tenants_response.status_code, 200)

    def test_admin_login_rejects_invalid_credentials(self) -> None:
        login_response = self.client.post(
            "/api/admin/auth/login",
            json={"username": "admin", "password": "wrong"},
        )
        self.assertEqual(login_response.status_code, 401)

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

    def test_managed_secret_delete(self) -> None:
        put_response = self.client.put(
            "/api/admin/secrets/temporary-secret",
            json={"value": "temp-value"},
            auth=("admin", "secret"),
        )
        self.assertEqual(put_response.status_code, 200)

        delete_response = self.client.delete(
            "/api/admin/secrets/temporary-secret",
            auth=("admin", "secret"),
        )
        self.assertEqual(delete_response.status_code, 204)

        resolve_response = self.client.post(
            "/api/admin/secrets/resolve",
            json={"secret_ref": "temporary-secret"},
            auth=("admin", "secret"),
        )
        self.assertEqual(resolve_response.status_code, 200)
        self.assertFalse(resolve_response.json()["resolved"])
        self.assertEqual(resolve_response.json()["source"], "missing")

        missing_delete_response = self.client.delete(
            "/api/admin/secrets/temporary-secret",
            auth=("admin", "secret"),
        )
        self.assertEqual(missing_delete_response.status_code, 404)

    def test_jira_connect_uses_managed_secret_when_env_not_set(self) -> None:
        os.environ.pop("JIRA_OAUTH_CLIENT_ID", None)
        os.environ.pop("JIRA_OAUTH_CLIENT_SECRET", None)

        self.client.put(
            "/api/admin/secrets/JIRA_OAUTH_CLIENT_ID",
            json={"value": "jira-client-id-managed"},
            auth=("admin", "secret"),
        )
        self.client.put(
            "/api/admin/secrets/JIRA_OAUTH_CLIENT_SECRET",
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
        class _FakeGitHubClient:
            def list_installation_repositories(self):  # noqa: ANN001
                return [
                    InstallationRepository(
                        full_name="example/repo-one",
                        html_url="https://github.com/example/repo-one",
                        default_branch="main",
                        private=False,
                    )
                ]

        with (
            patch("orchestrator.api.routes_admin._refresh_jira_connection_tokens", return_value="access-token"),
            patch("orchestrator.api.routes_admin._jira_oauth_client", return_value=_FakeJiraClient()),
            patch("orchestrator.api.routes_admin.github_client_from_tenant_config", return_value=_FakeGitHubClient()),
        ):
            jira_test = self.client.post(
                "/api/admin/tenants/tenant-a/test-jira",
                auth=("admin", "secret"),
            )
        self.assertEqual(jira_test.status_code, 200)
        self.assertTrue(jira_test.json()["ok"])

        with patch("orchestrator.api.routes_admin.github_client_from_tenant_config", return_value=_FakeGitHubClient()):
            github_test = self.client.post(
                "/api/admin/tenants/tenant-a/test-github",
                auth=("admin", "secret"),
            )
        self.assertEqual(github_test.status_code, 200)
        self.assertTrue(github_test.json()["ok"])
        self.assertIn("1 repository/repositories accessible", github_test.json()["details"])

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

    def test_create_tenant_allows_missing_repository_during_onboarding(self) -> None:
        payload = self._tenant_payload()
        payload["repos"]["github_repository"] = None

        response = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(response.status_code, 201)
        self.assertIsNone(response.json()["repos"]["github_repository"])

    def test_create_tenant_blocks_when_codex_assets_invalid(self) -> None:
        payload = self._tenant_payload()
        with patch(
            "orchestrator.api.routes_admin.validate_enforcement_assets",
            side_effect=EnforcementAssetsError("version mismatch"),
        ):
            response = self.client.post(
                "/api/admin/tenants",
                json=payload,
                auth=("admin", "secret"),
            )

        self.assertEqual(response.status_code, 503)
        self.assertIn("Codex assets validation failed", response.json()["detail"])

    def test_update_tenant_blocks_when_codex_assets_invalid(self) -> None:
        payload = self._tenant_payload()
        create_response = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_response.status_code, 201)

        payload["name"] = "Tenant Updated"
        with patch(
            "orchestrator.api.routes_admin.validate_enforcement_assets",
            side_effect=EnforcementAssetsError("missing packaged asset"),
        ):
            update_response = self.client.put(
                "/api/admin/tenants/tenant-a",
                json=payload,
                auth=("admin", "secret"),
            )

        self.assertEqual(update_response.status_code, 503)
        self.assertIn("Codex assets validation failed", update_response.json()["detail"])

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
        self.assertIn(
            "/tenants/new/github?tenant_id=tenant-a&github_install=success",
            callback_response.headers.get("location", ""),
        )

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

    def test_list_github_repositories_returns_400_for_invalid_private_key(self) -> None:
        payload = self._tenant_payload()
        payload["github"]["installation_id"] = "12345"
        create_response = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_response.status_code, 201)

        with patch(
            "orchestrator.api.routes_admin.github_client_from_tenant_config",
            side_effect=ValueError("Invalid GitHub App private key secret"),
        ):
            response = self.client.get(
                "/api/admin/tenants/tenant-a/github/repositories",
                auth=("admin", "secret"),
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Invalid GitHub App private key secret", response.json()["detail"])

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

            def ensure_webhook(self, **_: object):  # noqa: ANN003
                return type(
                    "Webhook",
                    (),
                    {"webhook_id": "1001"},
                )()

            def ensure_webhook(self, **_: object):  # noqa: ANN003
                return type(
                    "Webhook",
                    (),
                    {"webhook_id": "1001"},
                )()

            def ensure_webhook(self, **_: object):  # noqa: ANN003
                return type(
                    "Webhook",
                    (),
                    {"webhook_id": "1002"},
                )()

            def ensure_webhook(self, **_: object):  # noqa: ANN003
                return type(
                    "Webhook",
                    (),
                    {"webhook_id": "1001"},
                )()

        with patch("orchestrator.api.routes_admin._jira_oauth_client", return_value=_FakeClient()):
            callback_response = self.client.get(
                "/api/admin/jira/connect/callback",
                params={"code": "abc123", "state": state_token},
                follow_redirects=False,
            )

        self.assertEqual(callback_response.status_code, 302)
        self.assertIn(
            "/tenants/new/jira?jira_oauth=success&jira_connection_id=",
            callback_response.headers.get("location", ""),
        )

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

            def ensure_webhook(self, **_: object):  # noqa: ANN003
                return type(
                    "Webhook",
                    (),
                    {"webhook_id": "1001"},
                )()

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
        self.assertEqual(tenant_response.json()["jira"]["webhook_provisioning"]["ok"], True)

    def test_provision_tenant_jira_webhooks_success(self) -> None:
        payload = self._tenant_payload()
        self._insert_jira_connection(connection_id="conn-1")
        create_response = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_response.status_code, 201)

        class _FakeClient:
            def ensure_webhook(self, **_: object):  # noqa: ANN003
                return type(
                    "Webhook",
                    (),
                    {"webhook_id": "2002"},
                )()

        with (
            patch("orchestrator.api.routes_admin._refresh_jira_connection_tokens", return_value="access-token"),
            patch("orchestrator.api.routes_admin._jira_oauth_client", return_value=_FakeClient()),
        ):
            response = self.client.post(
                "/api/admin/tenants/tenant-a/jira/webhooks/provision",
                auth=("admin", "secret"),
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["ok"], True)
        self.assertEqual(body["webhook_id"], "2002")

        tenant_response = self.client.get("/api/admin/tenants/tenant-a", auth=("admin", "secret"))
        self.assertEqual(tenant_response.status_code, 200)
        self.assertEqual(tenant_response.json()["jira"]["webhook_provisioning"]["webhook_id"], "2002")

    def test_provision_tenant_jira_webhooks_permission_failure_is_persisted(self) -> None:
        payload = self._tenant_payload()
        self._insert_jira_connection(connection_id="conn-1")
        create_response = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_response.status_code, 201)

        class _FakeClient:
            def ensure_webhook(self, **_: object):  # noqa: ANN003
                raise ValueError("Forbidden: missing Jira admin permission")

        with (
            patch("orchestrator.api.routes_admin._refresh_jira_connection_tokens", return_value="access-token"),
            patch("orchestrator.api.routes_admin._jira_oauth_client", return_value=_FakeClient()),
        ):
            response = self.client.post(
                "/api/admin/tenants/tenant-a/jira/webhooks/provision",
                auth=("admin", "secret"),
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["ok"], False)
        self.assertIn("missing Jira admin permission", body["details"])

        tenant_response = self.client.get("/api/admin/tenants/tenant-a", auth=("admin", "secret"))
        self.assertEqual(tenant_response.status_code, 200)
        self.assertEqual(tenant_response.json()["jira"]["webhook_provisioning"]["ok"], False)
        self.assertIn(
            "missing Jira admin permission",
            tenant_response.json()["jira"]["webhook_provisioning"]["error"],
        )

    def test_list_discord_allowlist_requests_returns_pending_requests(self) -> None:
        payload = self._tenant_payload()
        create_response = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_response.status_code, 201)

        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            tenant = session.get(Tenant, "tenant-a")
            self.assertIsNotNone(tenant)
            tenant.discord_config = {
                "channel_id": "discord-channel-1",
                "notify_events": ["run_started"],
                "allowlist_requests": [
                    {
                        "user_id": "discord-user-123",
                        "requested_at": datetime.now(timezone.utc).isoformat(),
                        "channel_id": "discord-channel-1",
                        "reason": "Need run access",
                    }
                ],
            }
            session.commit()

        response = self.client.get(
            "/api/admin/tenants/tenant-a/discord/allowlist-requests",
            auth=("admin", "secret"),
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]["user_id"], "discord-user-123")
        self.assertEqual(body[0]["reason"], "Need run access")

    def test_approve_discord_allowlist_request_notifies_and_updates_tenant(self) -> None:
        payload = self._tenant_payload()
        create_response = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_response.status_code, 201)

        session_factory = create_session_factory(self.database_url)

        with session_factory() as session:
            tenant = session.get(Tenant, "tenant-a")
            self.assertIsNotNone(tenant)
            tenant.discord_config = {
                "channel_id": "discord-channel-1",
                "notify_events": ["run_started"],
                "allowed_user_ids": [],
                "allowlist_requests": [
                    {
                        "user_id": "discord-user-456",
                        "requested_at": datetime.now(timezone.utc).isoformat(),
                        "channel_id": "discord-channel-1",
                        "reason": "Need sensitive commands",
                    }
                ],
            }
            session.commit()

        seed_token_secret = self.client.put(
            "/api/admin/secrets/DISCORD_BOT_TOKEN",
            json={"value": "test-discord-bot-token"},
            auth=("admin", "secret"),
        )
        self.assertEqual(seed_token_secret.status_code, 200)

        with patch("orchestrator.api.routes_admin.DiscordApiClient.send_direct_message", return_value={"id": "msg-1"}):
            response = self.client.post(
                "/api/admin/tenants/tenant-a/discord/allowlist-requests/discord-user-456/approve",
                auth=("admin", "secret"),
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertTrue(response.json()["notified"])

        tenant_response = self.client.get("/api/admin/tenants/tenant-a", auth=("admin", "secret"))
        self.assertEqual(tenant_response.status_code, 200)
        discord = tenant_response.json()["discord"]
        self.assertIn("discord-user-456", discord.get("allowed_user_ids", []))


if __name__ == "__main__":
    unittest.main()
