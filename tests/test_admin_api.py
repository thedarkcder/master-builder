import os
import unittest
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch
from types import SimpleNamespace

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select

from orchestrator.api.main import create_app
from orchestrator.api.routes.admin import _resolve_project_discord_channel_name
from orchestrator.core.config import get_settings
from orchestrator.core.enforcement_context import EnforcementAssetsError
from orchestrator.core.secrets import encrypt_value
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import JiraOAuthConnection, Project, Run, Tenant
from orchestrator.core.webhook_health import (
    reset_webhook_health_tracker_for_tests,
    webhook_health_tracker,
)
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
        os.environ["ORCHESTRATOR_GITHUB_APP_SLUG"] = "master-builder-app"
        os.environ["ORCHESTRATOR_SECRETS_ENCRYPTION_KEY"] = Fernet.generate_key().decode("utf-8")

        os.environ["GITHUB_APP_ID"] = "12345"
        os.environ["GITHUB_APP_PRIVATE_KEY"] = "not-a-real-key-for-tests"
        os.environ["JIRA_OAUTH_CLIENT_ID"] = "jira-client-id"
        os.environ["JIRA_OAUTH_CLIENT_SECRET"] = "jira-client-secret"

        get_settings.cache_clear()
        reset_db_engine_cache()
        reset_webhook_health_tracker_for_tests()
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
        os.environ.pop("GITHUB_APP_PRIVATE_KEY", None)
        os.environ.pop("JIRA_OAUTH_CLIENT_ID", None)
        os.environ.pop("JIRA_OAUTH_CLIENT_SECRET", None)

        os.environ.pop("ORCHESTRATOR_GITHUB_INSTALL_STATE_SECRET", None)
        os.environ.pop("ORCHESTRATOR_ADMIN_UI_BASE_URL", None)
        os.environ.pop("ORCHESTRATOR_PUBLIC_API_BASE_URL", None)
        os.environ.pop("ORCHESTRATOR_JIRA_OAUTH_STATE_SECRET", None)
        os.environ.pop("ORCHESTRATOR_GITHUB_APP_SLUG", None)
        os.environ.pop("ORCHESTRATOR_SECRETS_ENCRYPTION_KEY", None)

        get_settings.cache_clear()
        reset_db_engine_cache()
        reset_webhook_health_tracker_for_tests()

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
        self.assertEqual(put_response.json()["secret_ref"], "platform/secret/github-webhook")
        self.assertEqual(put_response.json()["source"], "managed")

        list_response = self.client.get("/api/admin/secrets", auth=("admin", "secret"))
        self.assertEqual(list_response.status_code, 200)
        refs = [item["secret_ref"] for item in list_response.json()]
        self.assertIn("platform/secret/github-webhook", refs)

        resolve_response = self.client.post(
            "/api/admin/secrets/resolve",
            json={"secret_ref": "secret/github-webhook"},
            auth=("admin", "secret"),
        )
        self.assertEqual(resolve_response.status_code, 200)
        self.assertTrue(resolve_response.json()["resolved"])
        self.assertEqual(resolve_response.json()["source"], "managed")

    def test_platform_secret_list_excludes_tenant_and_project_scoped_refs(self) -> None:
        create_response = self.client.post(
            "/api/admin/tenants",
            json=self._tenant_payload(),
            auth=("admin", "secret"),
        )
        self.assertEqual(create_response.status_code, 201)

        self.client.put(
            "/api/admin/secrets/platform%2FDISCORD_BOT_TOKEN",
            json={"value": "platform-token"},
            auth=("admin", "secret"),
        )
        self.client.put(
            "/api/admin/tenants/tenant-a/secrets/DISCORD_BOT_TOKEN",
            json={"value": "tenant-token"},
            auth=("admin", "secret"),
        )

        platform_response = self.client.get("/api/admin/secrets", auth=("admin", "secret"))
        self.assertEqual(platform_response.status_code, 200)
        platform_refs = {item["secret_ref"] for item in platform_response.json()}
        self.assertIn("platform/DISCORD_BOT_TOKEN", platform_refs)
        self.assertNotIn("tenant/tenant-a/DISCORD_BOT_TOKEN", platform_refs)

        tenant_response = self.client.get("/api/admin/tenants/tenant-a/secrets", auth=("admin", "secret"))
        self.assertEqual(tenant_response.status_code, 200)
        tenant_refs = {item["secret_ref"] for item in tenant_response.json()}
        self.assertIn("tenant/tenant-a/DISCORD_BOT_TOKEN", tenant_refs)

    def test_platform_secrets_endpoint_rejects_tenant_scoped_secret_ref(self) -> None:
        response = self.client.put(
            "/api/admin/secrets/tenant%2Ftenant-a%2FDISCORD_BOT_TOKEN",
            json={"value": "tenant-token"},
            auth=("admin", "secret"),
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Platform secrets must use platform/* refs", response.json()["detail"])

    def test_platform_secret_resolve_rejects_tenant_scoped_secret_ref(self) -> None:
        response = self.client.post(
            "/api/admin/secrets/resolve",
            json={"secret_ref": "tenant/tenant-a/DISCORD_BOT_TOKEN"},
            auth=("admin", "secret"),
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Platform secrets must use platform/* refs", response.json()["detail"])

    def test_tenant_secret_endpoints_require_existing_tenant(self) -> None:
        list_response = self.client.get("/api/admin/tenants/missing/secrets", auth=("admin", "secret"))
        self.assertEqual(list_response.status_code, 404)
        self.assertIn("Tenant not found", list_response.json()["detail"])

        put_response = self.client.put(
            "/api/admin/tenants/missing/secrets/DISCORD_BOT_TOKEN",
            json={"value": "token"},
            auth=("admin", "secret"),
        )
        self.assertEqual(put_response.status_code, 404)

        resolve_response = self.client.post(
            "/api/admin/tenants/missing/secrets/resolve",
            json={"secret_ref": "DISCORD_BOT_TOKEN"},
            auth=("admin", "secret"),
        )
        self.assertEqual(resolve_response.status_code, 404)

        delete_response = self.client.delete(
            "/api/admin/tenants/missing/secrets/DISCORD_BOT_TOKEN",
            auth=("admin", "secret"),
        )
        self.assertEqual(delete_response.status_code, 404)

    def test_tenant_secret_rejects_prefixed_secret_key(self) -> None:
        create_response = self.client.post(
            "/api/admin/tenants",
            json=self._tenant_payload(),
            auth=("admin", "secret"),
        )
        self.assertEqual(create_response.status_code, 201)

        response = self.client.put(
            "/api/admin/tenants/tenant-a/secrets/platform%2FDISCORD_BOT_TOKEN",
            json={"value": "token"},
            auth=("admin", "secret"),
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Tenant secret key must not include a scope prefix", response.json()["detail"])

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
        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            projects = session.execute(
                select(Project).where(Project.tenant_id == "tenant-a")
            ).scalars().all()
            self.assertEqual(len(projects), 1)
            self.assertEqual(projects[0].project_id, "tenant-a-default")
            self.assertEqual(projects[0].github_repository, "https://github.com/example/repo")
            self.assertEqual(projects[0].jira_project_key, "TP")

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
            patch("orchestrator.api.routes.admin._refresh_jira_connection_tokens", return_value="access-token"),
            patch("orchestrator.api.routes.admin._jira_oauth_client", return_value=_FakeJiraClient()),
            patch("orchestrator.api.routes.admin.github_client_from_tenant_config", return_value=_FakeGitHubClient()),
        ):
            jira_test = self.client.post(
                "/api/admin/tenants/tenant-a/test-jira",
                auth=("admin", "secret"),
            )
        self.assertEqual(jira_test.status_code, 200)
        self.assertTrue(jira_test.json()["ok"])

        with patch("orchestrator.api.routes.admin.github_client_from_tenant_config", return_value=_FakeGitHubClient()):
            github_test = self.client.post(
                "/api/admin/tenants/tenant-a/test-github",
                auth=("admin", "secret"),
            )
        self.assertEqual(github_test.status_code, 200)
        self.assertTrue(github_test.json()["ok"])
        self.assertEqual(
            github_test.json()["details"],
            "GitHub tenant configuration looks valid and secret refs resolve",
        )

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
            patch("orchestrator.api.routes.admin._refresh_jira_connection_tokens", return_value="access-token"),
            patch("orchestrator.api.routes.admin._jira_oauth_client", return_value=fake_client),
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

    def test_release_bootstrap_persists_success_report(self) -> None:
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
                return []

        with (
            patch("orchestrator.api.routes.admin._refresh_jira_connection_tokens", return_value="access-token"),
            patch("orchestrator.api.routes.admin._jira_oauth_client", return_value=_FakeJiraClient()),
        ):
            response = self.client.post(
                "/api/admin/tenants/tenant-a/release/bootstrap",
                auth=("admin", "secret"),
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["checks"]["jira_connection"])
        self.assertTrue(body["checks"]["jira_project_keys"])
        self.assertTrue(body["checks"]["jira_required_statuses"])
        self.assertTrue(body["checks"]["github_installation"])

        persisted = self.client.get(
            "/api/admin/tenants/tenant-a/release/bootstrap",
            auth=("admin", "secret"),
        )
        self.assertEqual(persisted.status_code, 200)
        self.assertTrue(persisted.json()["ok"])
        self.assertEqual(persisted.json()["checks"]["jira_required_statuses"], True)

    def test_release_bootstrap_reports_missing_configuration(self) -> None:
        payload = self._tenant_payload()
        payload["jira"]["connection_id"] = None
        payload["jira"]["project_keys"] = []
        payload["github"]["installation_id"] = None

        create_response = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_response.status_code, 201)

        response = self.client.post(
            "/api/admin/tenants/tenant-a/release/bootstrap",
            auth=("admin", "secret"),
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["ok"])
        self.assertFalse(body["checks"]["jira_connection"])
        self.assertFalse(body["checks"]["jira_project_keys"])
        self.assertFalse(body["checks"]["github_installation"])
        self.assertIn("Missing Jira project keys.", body["details"])

    def test_project_crud_and_uniqueness(self) -> None:
        payload = self._tenant_payload()
        self._insert_jira_connection(connection_id="conn-1")
        create_tenant = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_tenant.status_code, 201)

        create_project = self.client.post(
            "/api/admin/tenants/tenant-a/projects",
            json={
                "name": "mobile-app",
                "github_repository": "https://github.com/example/mobile-app",
                "jira_project_key": "MBAPP",
                "environment": {"APP_ENV": "prod"},
                "secret_refs": {"API_TOKEN": "RUNNER_TOKEN"},
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(create_project.status_code, 201)
        project_id = create_project.json()["project_id"]
        self.assertEqual(create_project.json()["jira_project_key"], "MBAPP")
        self.assertEqual(create_project.json()["environment"], {"APP_ENV": "prod"})
        self.assertEqual(create_project.json()["secret_refs"], {"API_TOKEN": "RUNNER_TOKEN"})
        self.assertIsNone(create_project.json()["discord"])

        duplicate_repo = self.client.post(
            "/api/admin/tenants/tenant-a/projects",
            json={
                "name": "dup",
                "github_repository": "https://github.com/example/mobile-app",
                "jira_project_key": "MBAPP2",
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(duplicate_repo.status_code, 409)

        list_projects = self.client.get("/api/admin/tenants/tenant-a/projects", auth=("admin", "secret"))
        self.assertEqual(list_projects.status_code, 200)
        self.assertEqual(len(list_projects.json()), 2)

        update_project = self.client.put(
            f"/api/admin/tenants/tenant-a/projects/{project_id}",
            json={
                "name": "mobile-app-renamed",
                "github_repository": "https://github.com/example/mobile-app-renamed",
                "jira_project_key": "MBAPP",
                "environment": {"APP_ENV": "stage"},
                "secret_refs": {"API_TOKEN": "RUNNER_TOKEN_NEXT"},
                "is_archived": True,
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(update_project.status_code, 200)
        self.assertTrue(update_project.json()["is_archived"])
        self.assertEqual(update_project.json()["environment"], {"APP_ENV": "stage"})
        self.assertEqual(update_project.json()["secret_refs"], {"API_TOKEN": "RUNNER_TOKEN_NEXT"})
        self.assertIsNone(update_project.json()["discord"])

    def test_project_discord_enable_provisions_channel_when_missing(self) -> None:
        payload = self._tenant_payload()
        self._insert_jira_connection(connection_id="conn-1")
        create_tenant = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_tenant.status_code, 201)

        projects_response = self.client.get("/api/admin/tenants/tenant-a/projects", auth=("admin", "secret"))
        self.assertEqual(projects_response.status_code, 200)
        default_project = projects_response.json()[0]
        project_id = default_project["project_id"]

        with patch(
            "orchestrator.api.routes.admin._resolve_project_discord_channel_binding",
            return_value={"channel_id": "discord-channel-proj-1", "notify_events": []},
        ) as provision_mock:
            response = self.client.put(
                f"/api/admin/tenants/tenant-a/projects/{project_id}",
                json={
                    "name": default_project["name"],
                    "github_repository": default_project["github_repository"],
                    "jira_project_key": default_project["jira_project_key"],
                    "discord": {"notify_events": []},
                    "is_archived": False,
                },
                auth=("admin", "secret"),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["discord"]["channel_id"], "discord-channel-proj-1")
        provision_mock.assert_called_once()

    def test_update_project_preserves_discord_allowlist_fields(self) -> None:
        payload = self._tenant_payload()
        self._insert_jira_connection(connection_id="conn-1")
        create_tenant = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_tenant.status_code, 201)

        projects_response = self.client.get("/api/admin/tenants/tenant-a/projects", auth=("admin", "secret"))
        self.assertEqual(projects_response.status_code, 200)
        default_project = projects_response.json()[0]
        project_id = default_project["project_id"]

        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            project = session.get(Project, project_id)
            assert project is not None
            project.discord_config = {
                "channel_id": "discord-channel-proj-1",
                "notify_events": ["run_started"],
                "allowed_user_ids": ["discord-user-1"],
                "allowlist_requests": [
                    {
                        "project_id": project_id,
                        "user_id": "discord-user-2",
                        "requested_at": "2026-02-09T12:00:00Z",
                    }
                ],
            }
            session.add(project)
            session.commit()

        with patch(
            "orchestrator.api.routes.admin._resolve_project_discord_channel_binding",
            side_effect=lambda **kwargs: dict(kwargs["discord_config"]),
        ):
            response = self.client.put(
                f"/api/admin/tenants/tenant-a/projects/{project_id}",
                json={
                    "name": default_project["name"],
                    "github_repository": default_project["github_repository"],
                    "jira_project_key": default_project["jira_project_key"],
                    "discord": {"notify_events": ["run_failed"]},
                    "is_archived": False,
                },
                auth=("admin", "secret"),
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["discord"]["notify_events"], ["run_failed"])

        with session_factory() as session:
            project = session.get(Project, project_id)
            assert project is not None
            discord_config = dict(project.discord_config or {})
            self.assertEqual(discord_config.get("allowed_user_ids"), ["discord-user-1"])
            self.assertEqual(len(discord_config.get("allowlist_requests", [])), 1)

    def test_project_discord_channel_name_template_appends_project_when_template_not_project_scoped(self) -> None:
        now = datetime.now(timezone.utc)
        tenant = Tenant(
            tenant_id="example",
            name="example",
            is_enabled=True,
            jira_config={},
            github_config={},
            repos_config={},
            policy_config={},
            discord_config={},
            created_at=now,
            updated_at=now,
        )
        project = Project(
            project_id="project-1",
            tenant_id="example",
            name="Master Builder API",
            github_repository="https://github.com/example/repo",
            jira_project_key="MAB",
            policy_overrides={},
            environment={},
            secret_refs={},
            discord_config={},
            is_archived=False,
            created_at=now,
            updated_at=now,
        )
        settings = SimpleNamespace(discord_channel_name_template="team-core")
        channel_name = _resolve_project_discord_channel_name(settings=settings, tenant=tenant, project=project)
        self.assertEqual(channel_name, "team-core-master-builder-api")

    def test_list_runs_supports_project_filter(self) -> None:
        payload = self._tenant_payload()
        self._insert_jira_connection(connection_id="conn-1")
        create_tenant = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_tenant.status_code, 201)

        create_project = self.client.post(
            "/api/admin/tenants/tenant-a/projects",
            json={
                "name": "mobile-app",
                "github_repository": "https://github.com/example/mobile-app",
                "jira_project_key": "MBAPP",
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(create_project.status_code, 201)
        created_project_id = create_project.json()["project_id"]

        session_factory = create_session_factory(self.database_url)
        now = datetime.now(timezone.utc)
        with session_factory() as session:
            session.add_all(
                [
                    Run(
                        run_id="run-default-project",
                        tenant_id="tenant-a",
                        project_id="tenant-a-default",
                        issue_key="TP-1",
                        issue_summary="Default project run",
                        issue_description="desc",
                        repo_url="https://github.com/example/repo",
                        branch=None,
                        pr_url=None,
                        status="queued",
                        last_error=None,
                        plan=None,
                        created_at=now,
                        started_at=None,
                        finished_at=None,
                    ),
                    Run(
                        run_id="run-created-project",
                        tenant_id="tenant-a",
                        project_id=created_project_id,
                        issue_key="MBAPP-2",
                        issue_summary="Created project run",
                        issue_description="desc",
                        repo_url="https://github.com/example/mobile-app",
                        branch=None,
                        pr_url=None,
                        status="running",
                        last_error=None,
                        plan=None,
                        created_at=now + timedelta(seconds=1),
                        started_at=now + timedelta(seconds=1),
                        finished_at=None,
                    ),
                ]
            )
            session.commit()

        all_runs_response = self.client.get("/api/admin/runs?tenant_id=tenant-a", auth=("admin", "secret"))
        self.assertEqual(all_runs_response.status_code, 200)
        self.assertEqual({run["run_id"] for run in all_runs_response.json()}, {"run-default-project", "run-created-project"})

        project_runs_response = self.client.get(
            f"/api/admin/runs?tenant_id=tenant-a&project_id={created_project_id}",
            auth=("admin", "secret"),
        )
        self.assertEqual(project_runs_response.status_code, 200)
        filtered_runs = project_runs_response.json()
        self.assertEqual(len(filtered_runs), 1)
        self.assertEqual(filtered_runs[0]["run_id"], "run-created-project")
        self.assertEqual(filtered_runs[0]["project_id"], created_project_id)

    def test_tenant_health_rollup_includes_integration_and_webhook_signals(self) -> None:
        payload = self._tenant_payload()
        payload["github"]["installation_id"] = "12345"
        create_tenant = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_tenant.status_code, 201)

        create_project = self.client.post(
            "/api/admin/tenants/tenant-a/projects",
            json={
                "name": "mobile-app",
                "github_repository": "https://github.com/example/mobile-app",
                "jira_project_key": "MBAPP",
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(create_project.status_code, 201)
        project_id = create_project.json()["project_id"]

        now = datetime.now(timezone.utc)
        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            tenant = session.get(Tenant, "tenant-a")
            assert tenant is not None
            jira_config = dict(tenant.jira_config or {})
            jira_config["webhook_last_received_at"] = now.isoformat()
            jira_config["webhook_last_error"] = None
            tenant.jira_config = jira_config
            session.add_all(
                [
                    Run(
                        run_id="health-run-queued",
                        tenant_id="tenant-a",
                        project_id=project_id,
                        issue_key="MBAPP-1",
                        issue_summary="Queued run",
                        issue_description="desc",
                        repo_url="https://github.com/example/mobile-app",
                        branch=None,
                        pr_url=None,
                        status="queued",
                        last_error=None,
                        plan=None,
                        created_at=now - timedelta(minutes=10),
                        started_at=None,
                        finished_at=None,
                    ),
                    Run(
                        run_id="health-run-failed",
                        tenant_id="tenant-a",
                        project_id=project_id,
                        issue_key="MBAPP-2",
                        issue_summary="Failed run",
                        issue_description="desc",
                        repo_url="https://github.com/example/mobile-app",
                        branch=None,
                        pr_url=None,
                        status="failed",
                        last_error="failed",
                        plan=None,
                        created_at=now - timedelta(minutes=8),
                        started_at=now - timedelta(minutes=7),
                        finished_at=now - timedelta(minutes=5),
                    ),
                    Run(
                        run_id="health-run-succeeded",
                        tenant_id="tenant-a",
                        project_id=project_id,
                        issue_key="MBAPP-3",
                        issue_summary="Succeeded run",
                        issue_description="desc",
                        repo_url="https://github.com/example/mobile-app",
                        branch=None,
                        pr_url=None,
                        status="succeeded",
                        last_error=None,
                        plan=None,
                        created_at=now - timedelta(minutes=4),
                        started_at=now - timedelta(minutes=3),
                        finished_at=now - timedelta(minutes=2),
                    ),
                ]
            )
            session.commit()

        webhook_health_tracker.record(tenant_id="tenant-a", outcome="received")
        webhook_health_tracker.record(tenant_id="tenant-a", outcome="received")
        webhook_health_tracker.record(tenant_id="tenant-a", outcome="failed")

        response = self.client.get("/api/admin/tenants/tenant-a/health", auth=("admin", "secret"))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["tenant_id"], "tenant-a")
        self.assertEqual(body["active_projects"], 2)
        self.assertEqual(body["active_agents"], 1)
        self.assertEqual(body["total_runs"], 3)
        self.assertEqual(body["failed_runs"], 1)
        self.assertAlmostEqual(body["run_failure_rate_ratio"], 1 / 3, places=6)
        self.assertAlmostEqual(body["average_task_duration_seconds"], 90.0, delta=1.0)
        self.assertEqual(body["webhook_events_received"], 2)
        self.assertEqual(body["webhook_events_failed"], 1)
        self.assertEqual(body["webhook_failure_rate_ratio"], 0.5)
        self.assertTrue(body["integrations"]["jira_connected"])
        self.assertTrue(body["integrations"]["github_connected"])
        self.assertTrue(body["integrations"]["jira_webhook_healthy"])

    def test_tenant_health_returns_404_for_missing_tenant(self) -> None:
        response = self.client.get("/api/admin/tenants/missing/health", auth=("admin", "secret"))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "Tenant not found")

    def test_github_secret_resolution_prefers_tenant_scope_over_platform(self) -> None:
        payload = self._tenant_payload()
        payload["github"]["installation_id"] = "12345"
        create_response = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_response.status_code, 201)

        self.client.put(
            "/api/admin/secrets/platform/GITHUB_APP_ID",
            json={"value": "platform-app-id"},
            auth=("admin", "secret"),
        )
        self.client.put(
            "/api/admin/secrets/platform/GITHUB_APP_PRIVATE_KEY",
            json={"value": "platform-private-key"},
            auth=("admin", "secret"),
        )
        self.client.put(
            "/api/admin/tenants/tenant-a/secrets/GITHUB_APP_ID",
            json={"value": "tenant-app-id"},
            auth=("admin", "secret"),
        )
        self.client.put(
            "/api/admin/tenants/tenant-a/secrets/GITHUB_APP_PRIVATE_KEY",
            json={"value": "tenant-private-key"},
            auth=("admin", "secret"),
        )

        captured: dict[str, str | None] = {}

        def _fake_factory(config: dict, *, secret_lookup):  # noqa: ANN001
            captured["app_id"] = secret_lookup(config["app_id_ref"])
            captured["private_key"] = secret_lookup(config["private_key_ref"])
            return object()

        with patch("orchestrator.api.routes.admin.github_client_from_tenant_config", side_effect=_fake_factory):
            response = self.client.post(
                "/api/admin/tenants/tenant-a/test-github",
                auth=("admin", "secret"),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["app_id"], "tenant-app-id")
        self.assertEqual(captured["private_key"], "tenant-private-key")

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

    def test_archive_and_unarchive_tenant(self) -> None:
        payload = self._tenant_payload()
        create_response = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_response.status_code, 201)
        self.assertTrue(create_response.json()["is_enabled"])

        archive_response = self.client.post(
            "/api/admin/tenants/tenant-a/archive",
            auth=("admin", "secret"),
        )
        self.assertEqual(archive_response.status_code, 200)
        self.assertFalse(archive_response.json()["is_enabled"])

        unarchive_response = self.client.post(
            "/api/admin/tenants/tenant-a/unarchive",
            auth=("admin", "secret"),
        )
        self.assertEqual(unarchive_response.status_code, 200)
        self.assertTrue(unarchive_response.json()["is_enabled"])

    def test_create_tenant_allows_empty_project_keys(self) -> None:
        payload = self._tenant_payload()
        payload["jira"]["project_keys"] = []

        response = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["jira"]["project_keys"], [])

    def test_archiving_last_active_project_clears_tenant_project_keys(self) -> None:
        payload = self._tenant_payload()
        create_response = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_response.status_code, 201)

        projects_response = self.client.get("/api/admin/tenants/tenant-a/projects", auth=("admin", "secret"))
        self.assertEqual(projects_response.status_code, 200)
        projects = projects_response.json()
        self.assertEqual(len(projects), 1)
        project = projects[0]

        archive_response = self.client.put(
            f"/api/admin/tenants/tenant-a/projects/{project['project_id']}",
            json={
                "name": project["name"],
                "github_repository": project["github_repository"],
                "jira_project_key": project["jira_project_key"],
                "policy_overrides": project.get("policy_overrides", {}),
                "is_archived": True,
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(archive_response.status_code, 200)
        self.assertTrue(archive_response.json()["is_archived"])

        tenant_response = self.client.get("/api/admin/tenants/tenant-a", auth=("admin", "secret"))
        self.assertEqual(tenant_response.status_code, 200)
        self.assertEqual(tenant_response.json()["jira"]["project_keys"], [])

    def test_create_tenant_allows_missing_repository_during_onboarding(self) -> None:
        payload = self._tenant_payload()
        payload["repos"]["allowlist"] = []
        payload["repos"]["mapping_rules_by_project_key"] = {}
        payload["repos"]["mapping_rules_by_component"] = {}
        payload["repos"]["fallback_repo"] = None

        response = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["repos"]["allowlist"], [])
        self.assertIsNone(response.json()["repos"]["fallback_repo"])

    def test_create_tenant_blocks_when_codex_assets_invalid(self) -> None:
        payload = self._tenant_payload()
        with patch(
            "orchestrator.api.routes.admin.validate_enforcement_assets",
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
            "orchestrator.api.routes.admin.validate_enforcement_assets",
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
            "/tenants/new?tenant_id=tenant-a&github_install=success",
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

        with patch("orchestrator.api.routes.admin.github_client_from_tenant_config", return_value=_FakeClient()):
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
            "orchestrator.api.routes.admin.github_client_from_tenant_config",
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

        with patch("orchestrator.api.routes.admin._jira_oauth_client", return_value=_FakeClient()):
            callback_response = self.client.get(
                "/api/admin/jira/connect/callback",
                params={"code": "abc123", "state": state_token},
                follow_redirects=False,
            )

        self.assertEqual(callback_response.status_code, 302)
        self.assertIn(
            "/tenants/new?jira_oauth=success&jira_connection_id=",
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

        with patch("orchestrator.api.routes.admin._jira_oauth_client", return_value=_FakeClient()):
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
        self.assertIsInstance(tenant_response.json()["jira"]["managed_webhook_ids"], list)

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
            def register_webhook(self, **_: object) -> list[int]:  # noqa: ANN003
                return [2002]

        with (
            patch("orchestrator.api.routes.admin._refresh_jira_connection_tokens", return_value="access-token"),
            patch("orchestrator.api.routes.admin._jira_oauth_client", return_value=_FakeClient()),
        ):
            response = self.client.post(
                "/api/admin/tenants/tenant-a/jira/webhooks/provision",
                auth=("admin", "secret"),
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["ok"], True)
        self.assertEqual(body["webhook_ids"], [2002])

        tenant_response = self.client.get("/api/admin/tenants/tenant-a", auth=("admin", "secret"))
        self.assertEqual(tenant_response.status_code, 200)
        self.assertEqual(tenant_response.json()["jira"]["managed_webhook_ids"], [2002])

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
            def register_webhook(self, **_: object) -> list[int]:  # noqa: ANN003
                raise ValueError("Forbidden: missing Jira admin permission")

        with (
            patch("orchestrator.api.routes.admin._refresh_jira_connection_tokens", return_value="access-token"),
            patch("orchestrator.api.routes.admin._jira_oauth_client", return_value=_FakeClient()),
        ):
            response = self.client.post(
                "/api/admin/tenants/tenant-a/jira/webhooks/provision",
                auth=("admin", "secret"),
            )

        self.assertEqual(response.status_code, 502)
        self.assertIn("missing Jira admin permission", response.json()["details"])

        tenant_response = self.client.get("/api/admin/tenants/tenant-a", auth=("admin", "secret"))
        self.assertEqual(tenant_response.status_code, 200)
        self.assertEqual(tenant_response.json()["jira"]["managed_webhook_ids"], [])
        self.assertIn(
            "missing Jira admin permission",
            tenant_response.json()["jira"]["webhook_last_error"],
        )

    def test_provision_tenant_jira_webhooks_recovers_after_limit_cleanup(self) -> None:
        payload = self._tenant_payload()
        self._insert_jira_connection(connection_id="conn-1")
        create_response = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_response.status_code, 201)

        second_payload = self._tenant_payload()
        second_payload["name"] = "Tenant B"
        second_create_response = self.client.post(
            "/api/admin/tenants",
            json=second_payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(second_create_response.status_code, 201)

        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            tenant_b = session.get(Tenant, "tenant-b")
            self.assertIsNotNone(tenant_b)
            jira_config = dict(tenant_b.jira_config)
            jira_config["managed_webhook_ids"] = [9002]
            tenant_b.jira_config = jira_config
            session.commit()

        deleted_batches: list[list[int]] = []

        class _FakeClient:
            def __init__(self) -> None:
                self.register_attempts = 0

            def register_webhook(self, **_: object) -> list[int]:  # noqa: ANN003
                self.register_attempts += 1
                if self.register_attempts == 1:
                    raise ValueError(
                        "Webhook registration did not return any webhook IDs "
                        "(webhookRegistrationResult errors: A maximum of 5 webhooks is allowed per app per user.)"
                    )
                return [3003]

            def list_webhooks(self, **_: object) -> list[dict]:  # noqa: ANN003
                return [{"id": 9001}, {"id": 9002}]

            def delete_webhooks(self, *, access_token: str, cloud_id: str, webhook_ids: list[int]) -> None:  # noqa: ANN001
                deleted_batches.append(list(webhook_ids))

        fake_client = _FakeClient()
        with (
            patch("orchestrator.api.routes.admin._refresh_jira_connection_tokens", return_value="access-token"),
            patch("orchestrator.api.routes.admin._jira_oauth_client", return_value=fake_client),
        ):
            response = self.client.post(
                "/api/admin/tenants/tenant-a/jira/webhooks/provision",
                auth=("admin", "secret"),
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["webhook_ids"], [3003])
        self.assertEqual(deleted_batches, [[9001]])
        self.assertIn("Deleted 1 unmanaged Jira webhook(s).", response.json()["details"])

        tenant_response = self.client.get("/api/admin/tenants/tenant-a", auth=("admin", "secret"))
        self.assertEqual(tenant_response.status_code, 200)
        self.assertEqual(tenant_response.json()["jira"]["managed_webhook_ids"], [3003])
        self.assertIsNone(tenant_response.json()["jira"]["webhook_last_error"])

    def test_provision_tenant_jira_webhooks_recovers_after_limit_by_rotating_current_tenant_ids(self) -> None:
        payload = self._tenant_payload()
        self._insert_jira_connection(connection_id="conn-1")
        create_response = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_response.status_code, 201)

        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            tenant_a = session.get(Tenant, "tenant-a")
            self.assertIsNotNone(tenant_a)
            jira_config = dict(tenant_a.jira_config)
            jira_config["managed_webhook_ids"] = [7001]
            tenant_a.jira_config = jira_config
            session.commit()

        deleted_batches: list[list[int]] = []

        class _FakeClient:
            def __init__(self) -> None:
                self.register_attempts = 0

            def register_webhook(self, **_: object) -> list[int]:  # noqa: ANN003
                self.register_attempts += 1
                if self.register_attempts == 1:
                    raise ValueError(
                        "Webhook registration did not return any webhook IDs "
                        "(webhookRegistrationResult errors: A maximum of 5 webhooks is allowed per app per user.)"
                    )
                return [7002]

            def list_webhooks(self, **_: object) -> list[dict]:  # noqa: ANN003
                return [{"id": 7001}]

            def delete_webhooks(self, *, access_token: str, cloud_id: str, webhook_ids: list[int]) -> None:  # noqa: ANN001
                deleted_batches.append(list(webhook_ids))

        fake_client = _FakeClient()
        with (
            patch("orchestrator.api.routes.admin._refresh_jira_connection_tokens", return_value="access-token"),
            patch("orchestrator.api.routes.admin._jira_oauth_client", return_value=fake_client),
        ):
            response = self.client.post(
                "/api/admin/tenants/tenant-a/jira/webhooks/provision",
                auth=("admin", "secret"),
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["webhook_ids"], [7002])
        self.assertEqual(deleted_batches, [[7001]])
        self.assertIn("Deleted 1 existing tenant Jira webhook(s).", response.json()["details"])

        tenant_response = self.client.get("/api/admin/tenants/tenant-a", auth=("admin", "secret"))
        self.assertEqual(tenant_response.status_code, 200)
        self.assertEqual(tenant_response.json()["jira"]["managed_webhook_ids"], [7002])
        self.assertIsNone(tenant_response.json()["jira"]["webhook_last_error"])

    def test_provision_tenant_jira_webhooks_recovers_after_limit_by_rotating_any_registered_webhook(self) -> None:
        payload = self._tenant_payload()
        self._insert_jira_connection(connection_id="conn-1")
        create_response = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_response.status_code, 201)

        second_payload = self._tenant_payload()
        second_payload["tenant_id"] = "tenant-b"
        second_payload["name"] = "Tenant B"
        second_payload["jira"]["connection_id"] = "conn-1"
        second_create_response = self.client.post(
            "/api/admin/tenants",
            json=second_payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(second_create_response.status_code, 201)

        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            tenant_b = session.get(Tenant, "tenant-b")
            self.assertIsNotNone(tenant_b)
            jira_config = dict(tenant_b.jira_config)
            jira_config["managed_webhook_ids"] = [8001]
            tenant_b.jira_config = jira_config
            session.commit()

        deleted_batches: list[list[int]] = []

        class _FakeClient:
            def __init__(self) -> None:
                self.register_attempts = 0

            def register_webhook(self, **_: object) -> list[int]:  # noqa: ANN003
                self.register_attempts += 1
                if self.register_attempts == 1:
                    raise ValueError(
                        "Webhook registration did not return any webhook IDs "
                        "(webhookRegistrationResult errors: A maximum of 5 webhooks is allowed per app per user.)"
                    )
                return [8002]

            def list_webhooks(self, **_: object) -> list[dict]:  # noqa: ANN003
                return [{"id": 8001}]

            def delete_webhooks(self, *, access_token: str, cloud_id: str, webhook_ids: list[int]) -> None:  # noqa: ANN001
                deleted_batches.append(list(webhook_ids))

        fake_client = _FakeClient()
        with (
            patch("orchestrator.api.routes.admin._refresh_jira_connection_tokens", return_value="access-token"),
            patch("orchestrator.api.routes.admin._jira_oauth_client", return_value=fake_client),
        ):
            response = self.client.post(
                "/api/admin/tenants/tenant-a/jira/webhooks/provision",
                auth=("admin", "secret"),
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["webhook_ids"], [8002])
        self.assertEqual(deleted_batches, [[8001]])
        self.assertIn("Deleted 1 rollover Jira webhook (8001) to free capacity.", response.json()["details"])

        tenant_a_response = self.client.get("/api/admin/tenants/tenant-a", auth=("admin", "secret"))
        self.assertEqual(tenant_a_response.status_code, 200)
        self.assertEqual(tenant_a_response.json()["jira"]["managed_webhook_ids"], [8002])

        tenant_b_response = self.client.get("/api/admin/tenants/tenant-b", auth=("admin", "secret"))
        self.assertEqual(tenant_b_response.status_code, 200)
        self.assertEqual(tenant_b_response.json()["jira"]["managed_webhook_ids"], [])

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
            project = session.get(Project, "tenant-a-default")
            self.assertIsNotNone(project)
            project.discord_config = {
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
            "/api/admin/tenants/tenant-a/projects/tenant-a-default/discord/allowlist-requests",
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
            project = session.get(Project, "tenant-a-default")
            self.assertIsNotNone(project)
            project.discord_config = {
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

        with patch("orchestrator.api.routes.admin.DiscordApiClient.send_direct_message", return_value={"id": "msg-1"}):
            response = self.client.post("/api/admin/tenants/tenant-a/projects/tenant-a-default/discord/allowlist-requests/discord-user-456/approve", auth=("admin", "secret"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertTrue(response.json()["notified"])

        with session_factory() as session:
            project = session.get(Project, "tenant-a-default")
            self.assertIsNotNone(project)
            allowed_user_ids = (project.discord_config or {}).get("allowed_user_ids", [])
            self.assertIn("discord-user-456", allowed_user_ids)

    def test_jira_webhook_lifecycle_endpoints(self) -> None:
        payload = self._tenant_payload()
        self._insert_jira_connection(connection_id="conn-1")
        create_response = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_response.status_code, 201)

        deleted_batches: list[list[int]] = []

        class _FakeJiraClient:
            def __init__(self) -> None:
                self._register_count = 0

            def register_webhook(  # noqa: ANN001
                self,
                *,
                access_token: str,
                cloud_id: str,
                callback_url: str,
                jql_filter: str,
                events: list[str],
            ) -> list[int]:
                self._register_count += 1
                if self._register_count == 1:
                    return [10101]
                return [20202]

            def delete_webhooks(self, *, access_token: str, cloud_id: str, webhook_ids: list[int]) -> None:  # noqa: ANN001
                deleted_batches.append(list(webhook_ids))

        fake_client = _FakeJiraClient()
        with (
            patch("orchestrator.api.routes.admin._refresh_jira_connection_tokens", return_value="access-token"),
            patch("orchestrator.api.routes.admin._jira_oauth_client", return_value=fake_client),
        ):
            provision = self.client.post(
                "/api/admin/tenants/tenant-a/jira/webhooks/provision",
                auth=("admin", "secret"),
            )
            self.assertEqual(provision.status_code, 200)
            self.assertTrue(provision.json()["ok"])
            self.assertEqual(provision.json()["webhook_ids"], [10101])

            diagnostics = self.client.get(
                "/api/admin/tenants/tenant-a/jira/webhooks/diagnostics",
                auth=("admin", "secret"),
            )
            self.assertEqual(diagnostics.status_code, 200)
            self.assertEqual(diagnostics.json()["managed_webhook_ids"], [10101])
            self.assertFalse(diagnostics.json()["recent_delivery_ok"])

            reset = self.client.post(
                "/api/admin/tenants/tenant-a/jira/webhooks/reset",
                auth=("admin", "secret"),
            )
            self.assertEqual(reset.status_code, 200)
            self.assertTrue(reset.json()["ok"])
            self.assertEqual(reset.json()["action"], "reset")
            self.assertEqual(reset.json()["webhook_ids"], [20202])
            self.assertEqual(deleted_batches, [[10101]])

            disconnect = self.client.post(
                "/api/admin/tenants/tenant-a/jira/disconnect",
                auth=("admin", "secret"),
            )
            self.assertEqual(disconnect.status_code, 200)
            self.assertTrue(disconnect.json()["ok"])
            self.assertEqual(disconnect.json()["action"], "disconnect")

        tenant_response = self.client.get("/api/admin/tenants/tenant-a", auth=("admin", "secret"))
        self.assertEqual(tenant_response.status_code, 200)
        self.assertIsNone(tenant_response.json()["jira"]["connection_id"])
        self.assertEqual(tenant_response.json()["jira"]["managed_webhook_ids"], [])

    def test_reset_tenant_jira_webhooks_recovers_single_url_conflict(self) -> None:
        payload = self._tenant_payload()
        self._insert_jira_connection(connection_id="conn-1")
        create_response = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_response.status_code, 201)

        deleted_batches: list[list[int]] = []

        class _FakeClient:
            def __init__(self) -> None:
                self.register_attempts = 0

            def register_webhook(self, **_: object) -> list[int]:  # noqa: ANN003
                self.register_attempts += 1
                if self.register_attempts == 1:
                    raise ValueError(
                        "Webhook registration did not return any webhook IDs "
                        "(webhookRegistrationResult errors: Only a single URL per user is allowed to be "
                        "registered via REST API. The currently used URL: "
                        "https://example.invalid/jira/webhook/girlpower)"
                    )
                return [33003]

            def list_webhooks(self, **_: object) -> list[dict]:  # noqa: ANN003
                return [
                    {
                        "id": 31001,
                        "url": "https://example.invalid/jira/webhook/girlpower",
                    }
                ]

            def delete_webhooks(self, *, access_token: str, cloud_id: str, webhook_ids: list[int]) -> None:  # noqa: ANN001
                deleted_batches.append(list(webhook_ids))

        with (
            patch("orchestrator.api.routes.admin._refresh_jira_connection_tokens", return_value="access-token"),
            patch("orchestrator.api.routes.admin._jira_oauth_client", return_value=_FakeClient()),
        ):
            response = self.client.post(
                "/api/admin/tenants/tenant-a/jira/webhooks/reset",
                auth=("admin", "secret"),
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["action"], "reset")
        self.assertEqual(response.json()["webhook_ids"], [33003])
        self.assertEqual(deleted_batches, [[31001]])
        self.assertIn("Deleted 1 conflicting Jira webhook URL subscription(s).", response.json()["details"])

    def test_reset_tenant_jira_webhooks_unhandled_error_returns_error_ref(self) -> None:
        payload = self._tenant_payload()
        self._insert_jira_connection(connection_id="conn-1")
        create_response = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_response.status_code, 201)

        with patch("orchestrator.api.routes.admin._provision_jira_webhook", side_effect=RuntimeError("boom")):
            with TestClient(create_app(), raise_server_exceptions=False) as non_raising_client:
                response = non_raising_client.post(
                    "/api/admin/tenants/tenant-a/jira/webhooks/reset",
                    auth=("admin", "secret"),
                )

        self.assertEqual(response.status_code, 500)
        detail = response.json().get("detail", "")
        self.assertTrue(detail.startswith("Internal server error. Ref: "))


if __name__ == "__main__":
    unittest.main()
