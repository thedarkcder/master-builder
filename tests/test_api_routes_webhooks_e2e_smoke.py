from __future__ import annotations

import os
import unittest
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from cryptography.fernet import Fernet
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from orchestrator.api.main import create_app
from orchestrator.api.schemas import DiscordCommandResponse
from orchestrator.core.config import get_settings
from orchestrator.core.secrets import encrypt_value
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import JiraOAuthConnection, Run, Tenant


@dataclass(frozen=True)
class RouteScenario:
    path: str
    auth: tuple[str, str] | None = None
    json: dict | None = None
    expected_statuses: tuple[int, ...] = (200,)


class _FakeJiraClient:
    def build_authorize_url(self, *, state: str) -> str:
        return f"https://jira.example/oauth?state={state}"

    def list_projects(self, *, access_token: str, cloud_id: str) -> list[SimpleNamespace]:
        return [SimpleNamespace(key="TP", name="Tenant Project")]

    def register_webhook(
        self,
        *,
        access_token: str,
        cloud_id: str,
        callback_url: str,
        jql_filter: str,
        events: list[str],
    ) -> list[int]:
        return [111]

    def delete_webhooks(self, *, access_token: str, cloud_id: str, webhook_ids: list[int]) -> None:
        return None

    def list_webhooks(self, *, access_token: str, cloud_id: str) -> list[dict]:
        return []

    def search_issues_by_jql(
        self,
        *,
        access_token: str,
        cloud_id: str,
        jql: str,
        max_results: int,
    ) -> list[SimpleNamespace]:
        return [SimpleNamespace(key="TP-1", summary="Issue summary", status="To Do")]


class _FakeGitHubClient:
    def get_installation_token(self) -> str:
        return "ghs_test_installation_token"

    def list_installation_repositories(self) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(
                full_name="example/repo",
                html_url="https://github.com/example/repo",
                default_branch="main",
                private=False,
            )
        ]


class ApiRoutesWebhooksE2ESmokeTests(unittest.TestCase):
    EXCLUDED_ROUTE_PATHS = {
        "/openapi.json",
        "/docs",
        "/docs/oauth2-redirect",
        "/redoc",
    }
    IGNORED_METHODS = {"HEAD", "OPTIONS"}

    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.database_url = f"sqlite:///{self.temp_dir.name}/api_e2e_smoke.db"

        os.environ["ORCHESTRATOR_DATABASE_URL"] = self.database_url
        os.environ["ORCHESTRATOR_ADMIN_USERNAME"] = "admin"
        os.environ["ORCHESTRATOR_ADMIN_PASSWORD"] = "secret"
        os.environ["ORCHESTRATOR_GITHUB_INSTALL_STATE_SECRET"] = "unit-test-secret"
        os.environ["ORCHESTRATOR_ADMIN_UI_BASE_URL"] = "http://localhost:4100"
        os.environ["ORCHESTRATOR_PUBLIC_API_BASE_URL"] = "http://localhost:4000"
        os.environ["ORCHESTRATOR_JIRA_OAUTH_STATE_SECRET"] = "jira-oauth-state-secret"
        os.environ["ORCHESTRATOR_GITHUB_APP_SLUG"] = "master-builder-app"
        os.environ["ORCHESTRATOR_SECRETS_ENCRYPTION_KEY"] = Fernet.generate_key().decode("utf-8")

        get_settings.cache_clear()
        reset_db_engine_cache()
        run_migrations(database_url=self.database_url)

        self.patch_stack = ExitStack()
        self.addCleanup(self.patch_stack.close)
        self.patch_stack.enter_context(
            patch(
                "orchestrator.api.routes.admin._jira_oauth_client",
                side_effect=lambda **_: _FakeJiraClient(),
            )
        )
        self.patch_stack.enter_context(
            patch(
                "orchestrator.api.routes.admin._refresh_jira_connection_tokens",
                return_value="access-token",
            )
        )
        self.patch_stack.enter_context(
            patch(
                "orchestrator.api.routes.admin.github_client_from_tenant_config",
                side_effect=lambda *_, **__: _FakeGitHubClient(),
            )
        )
        self.patch_stack.enter_context(
            patch(
                "orchestrator.api.routes.admin.ensure_project_checkout",
                return_value=None,
            )
        )
        self.patch_stack.enter_context(
            patch(
                "orchestrator.api.routes.admin._resolve_project_discord_channel_binding",
                side_effect=lambda **kwargs: dict(kwargs.get("discord_config") or {}),
            )
        )
        self.patch_stack.enter_context(
            patch(
                "orchestrator.api.routes.discord.execute_tenant_command_ingress",
                return_value=DiscordCommandResponse(ok=True, command="ask", message="ok", data=None),
            )
        )
        self.patch_stack.enter_context(
            patch(
                "orchestrator.api.routes.webhook_discord.execute_discord_ingress_command",
                return_value=DiscordCommandResponse(ok=True, command="ask", message="ok", data=None),
            )
        )
        self.patch_stack.enter_context(
            patch(
                "orchestrator.api.routes.webhook.ingest_jira_webhook_event",
                new=AsyncMock(return_value={"ok": True}),
            )
        )
        self.patch_stack.enter_context(
            patch(
                "orchestrator.api.routes.webhook_github.ingest_github_webhook_event",
                new=AsyncMock(return_value=JSONResponse(status_code=200, content={"ok": True})),
            )
        )
        self.patch_stack.enter_context(
            patch(
                "orchestrator.api.routes.webhook_discord_interactions._read_json_payload",
                new=AsyncMock(return_value=({"type": 1}, b"{}")),
            )
        )
        self.patch_stack.enter_context(
            patch(
                "orchestrator.api.routes.webhook_discord_interactions._resolve_discord_interactions_public_key",
                return_value=b"k",
            )
        )
        self.patch_stack.enter_context(
            patch(
                "orchestrator.api.routes.webhook_discord_interactions._validate_discord_interaction_signature",
                return_value=None,
            )
        )

        self.app = create_app()
        self.client = TestClient(self.app, raise_server_exceptions=False)

        self._seed_platform_secret("GITHUB_APP_ID", "12345")
        self._seed_platform_secret("GITHUB_APP_PRIVATE_KEY", "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----")
        self._seed_platform_secret("GITHUB_APP_SLUG", "master-builder-app")
        self._seed_platform_secret("JIRA_OAUTH_CLIENT_ID", "jira-client-id")
        self._seed_platform_secret("JIRA_OAUTH_CLIENT_SECRET", "jira-client-secret")
        self._seed_platform_secret("DISCORD_INTERACTIONS_PUBLIC_KEY", "0" * 64)
        self._seed_platform_secret("DISCORD_BOT_TOKEN", "discord-token")
        self._seed_platform_secret("DELETE_ME", "remove-me")

        self._create_tenant("example")
        self._create_tenant("Tenant Delete")
        self._prepare_runtime_records()

    def tearDown(self) -> None:
        self.client.close()
        self.temp_dir.cleanup()
        get_settings.cache_clear()
        reset_db_engine_cache()

    def _seed_platform_secret(self, secret_ref: str, value: str) -> None:
        if not secret_ref.startswith("platform/"):
            secret_ref = f"platform/{secret_ref}"
        encoded_secret_ref = secret_ref.replace("/", "%2F")
        response = self.client.put(
            f"/api/admin/secrets/{encoded_secret_ref}",
            json={"value": value},
            auth=("admin", "secret"),
        )
        if response.status_code != 200:
            raise RuntimeError(f"Failed seeding secret {secret_ref}: {response.status_code} {response.text}")

    def _create_tenant(self, name: str) -> None:
        payload = {
            "name": name,
            "is_enabled": True,
            "jira": {
                "project_keys": ["TP"],
                "ready_statuses": ["To Do"],
            },
            "github": {
                "mode": "github_app",
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
            "discord": {"channel_id": "discord-channel-1", "notify_events": ["run_started"]},
        }
        response = self.client.post("/api/admin/tenants", json=payload, auth=("admin", "secret"))
        if response.status_code != 201:
            raise RuntimeError(f"Failed creating tenant {name}: {response.status_code} {response.text}")

    def _prepare_runtime_records(self) -> None:
        session_factory = create_session_factory(self.database_url)
        settings = get_settings()
        now = datetime.now(timezone.utc)
        with session_factory() as session:
            tenant = session.get(Tenant, "example")
            if tenant is None:
                raise RuntimeError("Missing seeded tenant example")
            tenant.jira_config = {
                **dict(tenant.jira_config or {}),
                "connection_id": "conn-e2e",
                "project_keys": ["TP"],
                "ready_statuses": ["To Do"],
                "ready_jql": 'project = TP AND status = "To Do"',
            }
            session.add(
                JiraOAuthConnection(
                    connection_id="conn-e2e",
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
            session.add(
                Run(
                    run_id="run-e2e",
                    tenant_id="example",
                    project_id="example-default",
                    issue_key="TP-1",
                    issue_summary="smoke",
                    issue_description=None,
                    repo_url="https://github.com/example/repo",
                    branch="feature/e2e",
                    pr_url=None,
                    status="queued",
                    last_error=None,
                    plan=None,
                    created_at=now,
                    started_at=None,
                    finished_at=None,
                )
            )
            session.commit()

    def _request(self, method: str, scenario: RouteScenario):
        kwargs: dict = {}
        if scenario.auth is not None:
            kwargs["auth"] = scenario.auth
        if scenario.json is not None:
            kwargs["json"] = scenario.json
        return self.client.request(method, scenario.path, **kwargs)

    @classmethod
    def _route_scenarios(cls) -> dict[tuple[str, str], RouteScenario]:
        admin = ("admin", "secret")
        return {
            ("POST", "/api/admin/auth/login"): RouteScenario(
                path="/api/admin/auth/login",
                json={"username": "admin", "password": "secret"},
                expected_statuses=(200,),
            ),
            ("GET", "/api/admin/auth/me"): RouteScenario(path="/api/admin/auth/me", auth=admin),
            ("GET", "/api/admin/github/install/callback"): RouteScenario(
                path="/api/admin/github/install/callback?state=bad&installation_id=1",
                expected_statuses=(400,),
            ),
            ("GET", "/api/admin/jira/connect/callback"): RouteScenario(
                path="/api/admin/jira/connect/callback?code=x&state=bad",
                expected_statuses=(400,),
            ),
            ("POST", "/api/admin/jira/connect/start"): RouteScenario(
                path="/api/admin/jira/connect/start?return_to=wizard",
                auth=admin,
            ),
            ("GET", "/api/admin/jira/connections/{connection_id}/projects"): RouteScenario(
                path="/api/admin/jira/connections/conn-e2e/projects",
                auth=admin,
            ),
            ("GET", "/api/admin/runs"): RouteScenario(path="/api/admin/runs", auth=admin),
            ("GET", "/api/admin/runs/{run_id}"): RouteScenario(path="/api/admin/runs/run-e2e", auth=admin),
            ("GET", "/api/admin/runs/{run_id}/events"): RouteScenario(
                path="/api/admin/runs/run-e2e/events",
                auth=admin,
            ),
            ("GET", "/api/admin/runs/{run_id}/logs"): RouteScenario(
                path="/api/admin/runs/run-e2e/logs",
                auth=admin,
            ),
            ("GET", "/api/admin/runs/{run_id}/events/stream"): RouteScenario(
                path="/api/admin/runs/run-missing/events/stream",
                auth=admin,
                expected_statuses=(404,),
            ),
            ("POST", "/api/admin/runs/{run_id}/rerun"): RouteScenario(
                path="/api/admin/runs/run-e2e/rerun",
                auth=admin,
                expected_statuses=(201, 409),
            ),
            ("POST", "/api/admin/runs/{run_id}/cancel"): RouteScenario(
                path="/api/admin/runs/run-e2e/cancel",
                auth=admin,
                expected_statuses=(200, 409),
            ),
            ("GET", "/api/admin/agents/activity"): RouteScenario(
                path="/api/admin/agents/activity?tenant_id=example&project_id=example-default",
                auth=admin,
            ),
            ("GET", "/api/admin/tenants/{tenant_id}/projects/{project_id}/metrics"): RouteScenario(
                path="/api/admin/tenants/example/projects/example-default/metrics",
                auth=admin,
            ),
            ("GET", "/api/admin/alerts/evaluate"): RouteScenario(
                path="/api/admin/alerts/evaluate?tenant_id=example",
                auth=admin,
            ),
            ("GET", "/api/admin/tenants/{tenant_id}/health"): RouteScenario(
                path="/api/admin/tenants/example/health",
                auth=admin,
            ),
            ("GET", "/api/admin/observability/platform"): RouteScenario(
                path="/api/admin/observability/platform",
                auth=admin,
            ),
            ("GET", "/api/admin/observability/tenants/{tenant_id}"): RouteScenario(
                path="/api/admin/observability/tenants/example",
                auth=admin,
            ),
            ("GET", "/api/admin/observability/tenants/{tenant_id}/projects/{project_id}"): RouteScenario(
                path="/api/admin/observability/tenants/example/projects/example-default",
                auth=admin,
            ),
            ("GET", "/api/admin/secrets"): RouteScenario(path="/api/admin/secrets", auth=admin),
            ("POST", "/api/admin/secrets/resolve"): RouteScenario(
                path="/api/admin/secrets/resolve",
                auth=admin,
                json={"secret_ref": "platform/GITHUB_APP_ID"},
            ),
            ("DELETE", "/api/admin/secrets/{secret_ref:path}"): RouteScenario(
                path="/api/admin/secrets/platform%2FDELETE_ME",
                auth=admin,
                expected_statuses=(204,),
            ),
            ("PUT", "/api/admin/secrets/{secret_ref:path}"): RouteScenario(
                path="/api/admin/secrets/platform%2FE2E_SET",
                auth=admin,
                json={"value": "abc"},
            ),
            ("GET", "/api/admin/tenants"): RouteScenario(path="/api/admin/tenants", auth=admin),
            ("POST", "/api/admin/tenants"): RouteScenario(
                path="/api/admin/tenants",
                auth=admin,
                json={
                    "name": "Tenant Create Route",
                    "is_enabled": True,
                    "jira": {"project_keys": ["TC"], "ready_statuses": ["To Do"]},
                    "github": {"mode": "github_app", "installation_id": "12345"},
                    "repos": {
                        "allowlist": ["https://github.com/example/repo"],
                        "mapping_rules_by_project_key": {"TC": "https://github.com/example/repo"},
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
                    "discord": {"channel_id": "discord-channel-1", "notify_events": []},
                },
                expected_statuses=(201,),
            ),
            ("DELETE", "/api/admin/tenants/{tenant_id}"): RouteScenario(
                path="/api/admin/tenants/tenant-delete",
                auth=admin,
                expected_statuses=(204,),
            ),
            ("GET", "/api/admin/tenants/{tenant_id}"): RouteScenario(path="/api/admin/tenants/example", auth=admin),
            ("PUT", "/api/admin/tenants/{tenant_id}"): RouteScenario(
                path="/api/admin/tenants/example",
                auth=admin,
                json={
                    "name": "example",
                    "is_enabled": True,
                    "jira": {
                        "connection_id": "conn-e2e",
                        "project_keys": ["TP"],
                        "ready_statuses": ["To Do"],
                        "ready_jql": 'project = TP AND status = "To Do"',
                    },
                    "github": {"mode": "github_app", "installation_id": "12345"},
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
                    "discord": {"channel_id": "discord-channel-1", "notify_events": ["run_started"]},
                },
            ),
            ("POST", "/api/admin/tenants/{tenant_id}/archive"): RouteScenario(
                path="/api/admin/tenants/example/archive",
                auth=admin,
            ),
            ("POST", "/api/admin/tenants/{tenant_id}/github/install/start"): RouteScenario(
                path="/api/admin/tenants/example/github/install/start?return_to=edit",
                auth=admin,
            ),
            ("GET", "/api/admin/tenants/{tenant_id}/github/repositories"): RouteScenario(
                path="/api/admin/tenants/example/github/repositories",
                auth=admin,
            ),
            ("POST", "/api/admin/tenants/{tenant_id}/jira/disconnect"): RouteScenario(
                path="/api/admin/tenants/example/jira/disconnect",
                auth=admin,
            ),
            ("GET", "/api/admin/tenants/{tenant_id}/jira/webhooks/diagnostics"): RouteScenario(
                path="/api/admin/tenants/example/jira/webhooks/diagnostics",
                auth=admin,
            ),
            ("POST", "/api/admin/tenants/{tenant_id}/jira/webhooks/provision"): RouteScenario(
                path="/api/admin/tenants/example/jira/webhooks/provision",
                auth=admin,
                expected_statuses=(200, 400, 502),
            ),
            ("POST", "/api/admin/tenants/{tenant_id}/jira/webhooks/reset"): RouteScenario(
                path="/api/admin/tenants/example/jira/webhooks/reset",
                auth=admin,
                expected_statuses=(200, 400, 502),
            ),
            ("GET", "/api/admin/tenants/{tenant_id}/projects"): RouteScenario(
                path="/api/admin/tenants/example/projects",
                auth=admin,
            ),
            ("POST", "/api/admin/tenants/{tenant_id}/projects"): RouteScenario(
                path="/api/admin/tenants/example/projects",
                auth=admin,
                json={
                    "name": "Project Added",
                    "github_repository": "https://github.com/example/repo-added",
                    "jira_project_key": "TPA",
                    "policy_overrides": {},
                    "environment": {},
                    "secret_refs": {},
                    "discord": {"notify_events": []},
                },
                expected_statuses=(201,),
            ),
            ("GET", "/api/admin/tenants/{tenant_id}/projects/{project_id}"): RouteScenario(
                path="/api/admin/tenants/example/projects/example-default",
                auth=admin,
            ),
            ("PUT", "/api/admin/tenants/{tenant_id}/projects/{project_id}"): RouteScenario(
                path="/api/admin/tenants/example/projects/example-default",
                auth=admin,
                json={
                    "name": "example Default",
                    "github_repository": "https://github.com/example/repo",
                    "jira_project_key": "TP",
                    "policy_overrides": {},
                    "environment": {},
                    "secret_refs": {},
                    "discord": {"notify_events": []},
                    "is_archived": False,
                },
            ),
            ("GET", "/api/admin/tenants/{tenant_id}/projects/{project_id}/discord/allowlist-requests"): RouteScenario(
                path="/api/admin/tenants/example/projects/example-default/discord/allowlist-requests",
                auth=admin,
            ),
            ("POST", "/api/admin/tenants/{tenant_id}/projects/{project_id}/discord/allowlist-requests/{user_id}/approve"): RouteScenario(
                path="/api/admin/tenants/example/projects/example-default/discord/allowlist-requests/u1/approve",
                auth=admin,
                expected_statuses=(200, 404),
            ),
            ("GET", "/api/admin/tenants/{tenant_id}/ready-preview"): RouteScenario(
                path="/api/admin/tenants/example/ready-preview?max_results=5",
                auth=admin,
            ),
            ("GET", "/api/admin/tenants/{tenant_id}/release/bootstrap"): RouteScenario(
                path="/api/admin/tenants/example/release/bootstrap",
                auth=admin,
            ),
            ("POST", "/api/admin/tenants/{tenant_id}/release/bootstrap"): RouteScenario(
                path="/api/admin/tenants/example/release/bootstrap",
                auth=admin,
            ),
            ("GET", "/api/admin/tenants/{tenant_id}/repo-bootstrap"): RouteScenario(
                path="/api/admin/tenants/example/repo-bootstrap",
                auth=admin,
            ),
            ("GET", "/api/admin/tenants/{tenant_id}/secrets"): RouteScenario(
                path="/api/admin/tenants/example/secrets",
                auth=admin,
            ),
            ("POST", "/api/admin/tenants/{tenant_id}/secrets/resolve"): RouteScenario(
                path="/api/admin/tenants/example/secrets/resolve",
                auth=admin,
                json={"secret_ref": "DISCORD_BOT_TOKEN"},
            ),
            ("DELETE", "/api/admin/tenants/{tenant_id}/secrets/{secret_key:path}"): RouteScenario(
                path="/api/admin/tenants/example/secrets/E2E_TENANT_SECRET",
                auth=admin,
                expected_statuses=(204, 404),
            ),
            ("PUT", "/api/admin/tenants/{tenant_id}/secrets/{secret_key:path}"): RouteScenario(
                path="/api/admin/tenants/example/secrets/E2E_TENANT_SECRET",
                auth=admin,
                json={"value": "abc"},
            ),
            ("POST", "/api/admin/tenants/{tenant_id}/test-github"): RouteScenario(
                path="/api/admin/tenants/example/test-github",
                auth=admin,
            ),
            ("POST", "/api/admin/tenants/{tenant_id}/test-jira"): RouteScenario(
                path="/api/admin/tenants/example/test-jira",
                auth=admin,
            ),
            ("POST", "/api/admin/tenants/{tenant_id}/unarchive"): RouteScenario(
                path="/api/admin/tenants/example/unarchive",
                auth=admin,
            ),
            ("POST", "/discord/command/{tenant_id}"): RouteScenario(
                path="/discord/command/example",
                json={"user_id": "u1", "command": "!ask test", "channel_id": "c1"},
            ),
            ("POST", "/discord/interactions"): RouteScenario(
                path="/discord/interactions",
                json={"type": 1},
            ),
            ("POST", "/discord/webhook/{tenant_id}"): RouteScenario(
                path="/discord/webhook/example",
                json={"user_id": "u1", "command": "!ask test", "channel_id": "c1"},
            ),
            ("POST", "/github/webhook"): RouteScenario(path="/github/webhook", json={"action": "opened"}),
            ("GET", "/health"): RouteScenario(path="/health"),
            ("GET", "/metrics"): RouteScenario(path="/metrics"),
            ("POST", "/jira/webhook/{tenant_id}"): RouteScenario(path="/jira/webhook/example", json={"webhookEvent": "jira:issue_updated"}),
            ("GET", "/runs/{run_id}"): RouteScenario(path="/runs/run-e2e", auth=admin),
        }

    def test_every_external_route_has_strict_e2e_scenario_and_no_500(self) -> None:
        scenarios = self._route_scenarios()
        discovered: set[tuple[str, str]] = set()
        for route in self.app.routes:
            methods = sorted((getattr(route, "methods", None) or []))
            path = getattr(route, "path", "")
            if path in self.EXCLUDED_ROUTE_PATHS:
                continue
            for method in methods:
                if method in self.IGNORED_METHODS:
                    continue
                discovered.add((method, path))

        self.assertSetEqual(
            discovered,
            set(scenarios.keys()),
            msg=(
                "Route scenario map is incomplete or stale. "
                "Every external route must be explicitly covered."
            ),
        )

        for key in sorted(scenarios.keys()):
            method, _ = key
            scenario = scenarios[key]
            response = self._request(method, scenario)
            self.assertIn(
                response.status_code,
                scenario.expected_statuses,
                msg=f"{method} {scenario.path} status={response.status_code} body={response.text[:300]}",
            )
            self.assertNotEqual(
                response.status_code,
                500,
                msg=f"{method} {scenario.path} returned 500: {response.text[:300]}",
            )


if __name__ == "__main__":
    unittest.main()
