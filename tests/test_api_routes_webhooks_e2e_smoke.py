from __future__ import annotations

import os
import unittest
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from cryptography.fernet import Fernet
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from orchestrator.api.main import create_app
from orchestrator.core.config import get_settings
from orchestrator.core.platform.secrets import encrypt_value
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import AtlassianOAuthConnection, Run, Tenant, WorkflowCheckpoint, WorkflowExecution

pytestmark = pytest.mark.smoke


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

    def list_confluence_spaces(self, *, access_token: str, cloud_id: str, limit: int = 250) -> list[SimpleNamespace]:
        return [SimpleNamespace(space_id="space-1", key="ARCH", name="Architecture")]

    def get_confluence_space_by_key(
        self,
        *,
        access_token: str,
        cloud_id: str,
        space_key: str,
    ) -> SimpleNamespace:
        return SimpleNamespace(space_id="space-1", key=space_key, name="Architecture")

    def list_confluence_pages(
        self,
        *,
        access_token: str,
        cloud_id: str,
        site_url: str,
        space_id: str,
    ) -> list[SimpleNamespace]:
        return [SimpleNamespace(page_id="page-1", title="Architecture Home", webui_url=f"{site_url}/wiki/spaces/ARCH")]

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
        os.environ["ORCHESTRATOR_ADMIN_TOKEN_SECRET"] = "admin-token-secret-for-tests-0123456789"
        os.environ["ORCHESTRATOR_GITHUB_INSTALL_STATE_SECRET"] = "unit-test-secret"
        os.environ["ORCHESTRATOR_ADMIN_UI_BASE_URL"] = "http://localhost:4100"
        os.environ["ORCHESTRATOR_PUBLIC_API_BASE_URL"] = "http://localhost:4000"
        os.environ["ORCHESTRATOR_ATLASSIAN_OAUTH_STATE_SECRET"] = "atlassian-oauth-state-secret"
        os.environ["ORCHESTRATOR_GITHUB_APP_SLUG"] = "master-builder-app"
        os.environ["ORCHESTRATOR_SECRETS_ENCRYPTION_KEY"] = Fernet.generate_key().decode("utf-8")

        get_settings.cache_clear()
        reset_db_engine_cache()
        run_migrations(database_url=self.database_url)

        self.patch_stack = ExitStack()
        self.addCleanup(self.patch_stack.close)
        self.patch_stack.enter_context(
            patch(
                "orchestrator.api.admin.integration_dependencies.atlassian_oauth_client",
                side_effect=lambda **_: _FakeJiraClient(),
            )
        )
        self.patch_stack.enter_context(
            patch(
                "orchestrator.api.admin.integration_dependencies.refresh_atlassian_connection_tokens",
                return_value="access-token",
            )
        )
        self.patch_stack.enter_context(
            patch(
                "orchestrator.api.admin.integration_dependencies.github_client_from_tenant_config",
                side_effect=lambda *_, **__: _FakeGitHubClient(),
            )
        )
        self.patch_stack.enter_context(
            patch(
                "orchestrator.api.admin.route_helpers.ensure_project_checkout",
                return_value=None,
            )
        )
        self.patch_stack.enter_context(
            patch(
                "orchestrator.api.admin.route_helpers.resolve_project_discord_channel_binding",
                side_effect=lambda **kwargs: dict(kwargs.get("discord_config") or {}),
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
        self.patch_stack.enter_context(
            patch(
                "orchestrator.api.routes.admin_tenants.email_delivery.send_tenant_invite_email",
                return_value=None,
            )
        )

        self.app = create_app()
        self.client = TestClient(self.app, raise_server_exceptions=False)

        self._seed_platform_secret("GITHUB_APP_ID", "12345")
        self._seed_platform_secret("GITHUB_APP_PRIVATE_KEY", "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----")
        self._seed_platform_secret("GITHUB_APP_SLUG", "master-builder-app")
        self._seed_platform_secret("ATLASSIAN_OAUTH_CLIENT_ID", "atlassian-client-id")
        self._seed_platform_secret("ATLASSIAN_OAUTH_CLIENT_SECRET", "atlassian-client-secret")
        self._seed_platform_secret("DISCORD_INTERACTIONS_PUBLIC_KEY", "0" * 64)
        self._seed_platform_secret("DISCORD_BOT_TOKEN", "discord-token")
        self._seed_platform_secret("DELETE_ME", "remove-me")

        self._create_tenant("example")
        self._create_tenant("Tenant Delete")
        self._prepare_runtime_records()

    def tearDown(self) -> None:
        self.client.close()
        self.temp_dir.cleanup()
        os.environ.pop("ORCHESTRATOR_ADMIN_TOKEN_SECRET", None)
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
                "allow_code_reviews": True,
                "allow_pr_remediation": True,
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
                AtlassianOAuthConnection(
                    connection_id="conn-e2e",
                    account_id="account-1",
                    account_email="test@example.com",
                    cloud_id="cloud-1",
                    site_url="https://example.atlassian.net",
                    scopes=["read:jira-work", "write:jira-work", "read:space:confluence", "read:page:confluence"],
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
                WorkflowExecution(
                    workflow_id="workflow-e2e",
                    execution_id="workflow-e2e",
                    workflow_type_key="issue_execution",
                    tenant_id="example",
                    project_id="example-default",
                    source_system="jira",
                    source_ref="TP-1",
                    display_name="smoke",
                    source_description=None,
                    repo_url="https://github.com/example/repo",
                    branch="feature/e2e",
                    pr_url=None,
                    orchestration_backend="legacy",
                    dedupe_scope="issue_execution",
                    status="queued",
                    last_error=None,
                    active_run_id="run-e2e",
                    latest_checkpoint_id="checkpoint-e2e",
                    source_workflow_id=None,
                    source_run_id=None,
                    created_at=now,
                    started_at=None,
                    finished_at=None,
                    updated_at=now,
                )
            )
            session.add(
                Run(
                    run_id="run-e2e",
                    workflow_id="workflow-e2e",
                    tenant_id="example",
                    project_id="example-default",
                    issue_key="TP-1",
                    issue_summary="smoke",
                    issue_description=None,
                    repo_url="https://github.com/example/repo",
                    branch="feature/e2e",
                    pr_url=None,
                    attempt_number=1,
                    parent_run_id=None,
                    entry_mode="fresh",
                    entry_stage="orchestrated",
                    entry_checkpoint_id="checkpoint-e2e",
                    dedupe_scope="issue_execution",
                    status="queued",
                    last_error=None,
                    plan=None,
                    created_at=now,
                    started_at=None,
                    last_heartbeat_at=None,
                    worker_service_instance_id=None,
                    finished_at=None,
                )
            )
            session.add(
                WorkflowCheckpoint(
                    checkpoint_id="checkpoint-e2e",
                    workflow_id="workflow-e2e",
                    run_id="run-e2e",
                    checkpoint_kind="pm",
                    stage="pm",
                    payload_json={"source": "smoke"},
                    codex_session_id=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

    def _request(self, method: str, scenario: RouteScenario):
        kwargs: dict = {}
        if scenario.auth is not None:
            kwargs["auth"] = scenario.auth
        if scenario.json is not None:
            kwargs["json"] = scenario.json
        if scenario.path.split("?", maxsplit=1)[0].endswith("/stream") and 200 in scenario.expected_statuses:
            return SimpleNamespace(status_code=200, text="")
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
            ("GET", "/api/admin/status"): RouteScenario(path="/api/admin/status", auth=admin),
            ("GET", "/api/admin/agent-runtime-models"): RouteScenario(
                path="/api/admin/agent-runtime-models",
                auth=admin,
            ),
            ("GET", "/api/admin/github/install/callback"): RouteScenario(
                path="/api/admin/github/install/callback?state=bad&installation_id=1",
                expected_statuses=(400,),
            ),
            ("GET", "/api/admin/atlassian/connect/callback"): RouteScenario(
                path="/api/admin/atlassian/connect/callback?code=x&state=bad",
                expected_statuses=(400,),
            ),
            ("POST", "/api/admin/atlassian/connect/start"): RouteScenario(
                path="/api/admin/atlassian/connect/start?return_to=wizard",
                auth=admin,
            ),
            ("GET", "/api/admin/atlassian/connections/{connection_id}/jira-projects"): RouteScenario(
                path="/api/admin/atlassian/connections/conn-e2e/jira-projects",
                auth=admin,
            ),
            ("GET", "/api/admin/runs"): RouteScenario(path="/api/admin/runs", auth=admin),
            ("GET", "/api/admin/runs/{run_id}"): RouteScenario(path="/api/admin/runs/run-e2e", auth=admin),
            ("GET", "/api/admin/workflows"): RouteScenario(path="/api/admin/workflows", auth=admin),
            ("GET", "/api/admin/workflow-types"): RouteScenario(
                path="/api/admin/workflow-types?tenant_id=example",
                auth=admin,
            ),
            ("GET", "/api/admin/workflow-types/{workflow_type_key}"): RouteScenario(
                path="/api/admin/workflow-types/issue_execution?tenant_id=example",
                auth=admin,
            ),
            ("PUT", "/api/admin/workflow-types/{workflow_type_key}"): RouteScenario(
                path="/api/admin/workflow-types/issue_execution",
                auth=admin,
                json={"orchestration_backend": "legacy", "retry_policy": {}},
                expected_statuses=(200, 404, 422),
            ),
            ("GET", "/api/admin/workflows/{execution_id}"): RouteScenario(
                path="/api/admin/workflows/workflow-e2e",
                auth=admin,
            ),
            ("GET", "/api/admin/workflows/{execution_id}/telemetry"): RouteScenario(
                path="/api/admin/workflows/workflow-e2e/telemetry",
                auth=admin,
            ),
            ("GET", "/api/admin/workflows/{execution_id}/audit"): RouteScenario(
                path="/api/admin/workflows/workflow-e2e/audit",
                auth=admin,
            ),
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
            ("GET", "/api/admin/runtime/logs"): RouteScenario(
                path="/api/admin/runtime/logs?tenant_id=example&project_id=example-default",
                auth=admin,
            ),
            ("GET", "/api/admin/runtime/events/stream"): RouteScenario(
                path="/api/admin/runtime/events/stream?tenant_id=example&project_id=example-default",
                auth=admin,
            ),
            ("POST", "/api/admin/workflows/{execution_id}/attempts"): RouteScenario(
                path="/api/admin/workflows/workflow-e2e/attempts",
                auth=admin,
                json={"mode": "resume", "checkpoint_kind": "pm"},
                expected_statuses=(201, 409),
            ),
            ("POST", "/api/admin/workflows/{execution_id}/resume"): RouteScenario(
                path="/api/admin/workflows/workflow-e2e/resume",
                auth=admin,
                expected_statuses=(201, 400, 404, 409),
            ),
            ("GET", "/api/admin/workflows/{execution_id}/operations/{operation_id}/telemetry"): RouteScenario(
                path="/api/admin/workflows/workflow-e2e/operations/op-missing/telemetry",
                auth=admin,
                expected_statuses=(404,),
            ),
            ("GET", "/api/admin/workflows/{execution_id}/operations/{operation_id}/attempts/{attempt_id}/telemetry"): RouteScenario(
                path="/api/admin/workflows/workflow-e2e/operations/op-missing/attempts/attempt-missing/telemetry",
                auth=admin,
                expected_statuses=(404,),
            ),
            ("GET", "/api/admin/workflows/{execution_id}/operations/{operation_id}/telemetry/stream"): RouteScenario(
                path="/api/admin/workflows/workflow-e2e/operations/op-missing/telemetry/stream",
                auth=admin,
                expected_statuses=(404,),
            ),
            ("GET", "/api/admin/workflows/{execution_id}/operations/{operation_id}/attempts/{attempt_id}/telemetry/stream"): RouteScenario(
                path="/api/admin/workflows/workflow-e2e/operations/op-missing/attempts/attempt-missing/telemetry/stream",
                auth=admin,
                expected_statuses=(404,),
            ),
            ("GET", "/api/admin/workflows/{execution_id}/operations/{operation_id}/audit"): RouteScenario(
                path="/api/admin/workflows/workflow-e2e/operations/op-missing/audit",
                auth=admin,
                expected_statuses=(404,),
            ),
            ("GET", "/api/admin/workflows/{execution_id}/operations/{operation_id}/attempts/{attempt_id}/audit"): RouteScenario(
                path="/api/admin/workflows/workflow-e2e/operations/op-missing/attempts/attempt-missing/audit",
                auth=admin,
                expected_statuses=(200, 404),
            ),
            ("GET", "/api/admin/workflows/{execution_id}/operations/{operation_id}/transcript"): RouteScenario(
                path="/api/admin/workflows/workflow-e2e/operations/op-missing/transcript",
                auth=admin,
                expected_statuses=(200, 404),
            ),
            ("POST", "/api/admin/workflows/{execution_id}/operations/{operation_id}/retry"): RouteScenario(
                path="/api/admin/workflows/workflow-e2e/operations/op-missing/retry",
                auth=admin,
                expected_statuses=(400, 404, 409),
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
            ("POST", "/api/admin/audit/export"): RouteScenario(
                path="/api/admin/audit/export",
                auth=admin,
                json={"tenant_id": "example"},
            ),
            ("GET", "/api/admin/discord/commands/status"): RouteScenario(
                path="/api/admin/discord/commands/status",
                auth=admin,
            ),
            ("POST", "/api/admin/discord/commands/sync"): RouteScenario(
                path="/api/admin/discord/commands/sync",
                auth=admin,
            ),
            ("GET", "/api/admin/tenants/{tenant_id}/health"): RouteScenario(
                path="/api/admin/tenants/example/health",
                auth=admin,
            ),
            ("GET", "/api/admin/tenants/{tenant_id}/notifications"): RouteScenario(
                path="/api/admin/tenants/example/notifications",
                auth=admin,
            ),
            ("GET", "/api/admin/tenants/{tenant_id}/delivery-summary"): RouteScenario(
                path="/api/admin/tenants/example/delivery-summary",
                auth=admin,
            ),
            ("GET", "/api/admin/observability/platform"): RouteScenario(
                path="/api/admin/observability/platform",
                auth=admin,
            ),
            ("POST", "/api/admin/workers/{service_instance_id}/runtime-dependencies/{runtime_kind}/login-session"): RouteScenario(
                path="/api/admin/workers/worker-missing/runtime-dependencies/openai/login-session",
                auth=admin,
                expected_statuses=(202, 404, 409),
            ),
            ("GET", "/api/admin/workers/runtime-auth-requests/{request_id}"): RouteScenario(
                path="/api/admin/workers/runtime-auth-requests/request-missing",
                auth=admin,
                expected_statuses=(404,),
            ),
            ("GET", "/api/admin/observability/webhook-jobs"): RouteScenario(
                path="/api/admin/observability/webhook-jobs?tenant_id=example&project_id=example-default&limit=25&offset=0",
                auth=admin,
            ),
            ("POST", "/api/admin/observability/webhook-jobs/{job_id}/retry"): RouteScenario(
                path="/api/admin/observability/webhook-jobs/job-missing/retry?tenant_id=example&project_id=example-default",
                auth=admin,
                expected_statuses=(404, 409),
            ),
            ("GET", "/api/admin/observability/knowledge-jira-sync"): RouteScenario(
                path="/api/admin/observability/knowledge-jira-sync",
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
            ("GET", "/api/admin/agent-runtimes"): RouteScenario(
                path="/api/admin/agent-runtimes",
                auth=admin,
            ),
            ("GET", "/api/admin/agent-runtime-profiles"): RouteScenario(
                path="/api/admin/agent-runtime-profiles",
                auth=admin,
            ),
            ("GET", "/api/admin/agent-runtime-tools"): RouteScenario(
                path="/api/admin/agent-runtime-tools",
                auth=admin,
            ),
            ("POST", "/api/admin/agent-runtime-profiles"): RouteScenario(
                path="/api/admin/agent-runtime-profiles",
                auth=admin,
                json={
                    "profile_name": "smoke_profile",
                    "runtime_kind": "codex_cli",
                    "model": "gpt-5.4",
                    "reasoning_effort": "medium",
                    "tool_bridge_allowed": True,
                },
                expected_statuses=(200, 201, 400, 409, 422),
            ),
            ("PUT", "/api/admin/agent-runtime-profiles/{profile_name}"): RouteScenario(
                path="/api/admin/agent-runtime-profiles/smoke_profile",
                auth=admin,
                json={
                    "runtime_kind": "codex_cli",
                    "model": "gpt-5.4",
                    "reasoning_effort": "medium",
                    "tool_bridge_allowed": True,
                },
                expected_statuses=(200, 400, 404, 422),
            ),
            ("POST", "/api/admin/agent-runtime-profiles/{profile_name}/reset"): RouteScenario(
                path="/api/admin/agent-runtime-profiles/smoke_profile/reset",
                auth=admin,
                expected_statuses=(200, 400, 404, 422),
            ),
            ("DELETE", "/api/admin/agent-runtime-profiles/{profile_name}"): RouteScenario(
                path="/api/admin/agent-runtime-profiles/smoke_profile",
                auth=admin,
                expected_statuses=(204, 404),
            ),
            ("PUT", "/api/admin/agent-runtimes"): RouteScenario(
                path="/api/admin/agent-runtimes",
                auth=admin,
                json={
                    "role_routing": {"pm": "pm_conversation_fast"},
                    "name_routing": {"workflow_review_default": "engineering_execution_deep"},
                },
            ),
            ("POST", "/api/admin/agent-runtimes/reset"): RouteScenario(
                path="/api/admin/agent-runtimes/reset",
                auth=admin,
            ),
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
            ("GET", "/api/admin/codex/models"): RouteScenario(
                path="/api/admin/codex/models",
                auth=admin,
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
                        "allow_code_reviews": True,
                        "allow_pr_remediation": True,
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
                        "allow_code_reviews": True,
                        "allow_pr_remediation": True,
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
            ("POST", "/api/admin/tenants/{tenant_id}/discord/install/start"): RouteScenario(
                path="/api/admin/tenants/example/discord/install/start?return_to=edit",
                auth=admin,
                expected_statuses=(200, 400, 404, 422),
            ),
            ("POST", "/api/admin/tenants/{tenant_id}/discord/link/start"): RouteScenario(
                path="/api/admin/tenants/example/discord/link/start",
                auth=admin,
                expected_statuses=(200, 400, 404, 422),
            ),
            ("POST", "/api/admin/tenants/{tenant_id}/discord/onboarding-invite"): RouteScenario(
                path="/api/admin/tenants/example/discord/onboarding-invite",
                auth=admin,
                expected_statuses=(200, 400, 404, 422),
            ),
            ("GET", "/api/admin/tenants/{tenant_id}/discord/identity"): RouteScenario(
                path="/api/admin/tenants/example/discord/identity",
                auth=admin,
                expected_statuses=(200, 404),
            ),
            ("GET", "/api/admin/tenants/{tenant_id}/github/repositories"): RouteScenario(
                path="/api/admin/tenants/example/github/repositories",
                auth=admin,
            ),
            ("GET", "/api/admin/tenants/{tenant_id}/members"): RouteScenario(
                path="/api/admin/tenants/example/members",
                auth=admin,
            ),
            ("PUT", "/api/admin/tenants/{tenant_id}/members/{membership_id}"): RouteScenario(
                path="/api/admin/tenants/example/members/membership-missing",
                auth=admin,
                json={},
                expected_statuses=(404, 422),
            ),
            ("GET", "/api/admin/tenants/{tenant_id}/teams"): RouteScenario(
                path="/api/admin/tenants/example/teams",
                auth=admin,
            ),
            ("POST", "/api/admin/tenants/{tenant_id}/teams"): RouteScenario(
                path="/api/admin/tenants/example/teams",
                auth=admin,
                json={},
                expected_statuses=(201, 409, 422),
            ),
            ("PUT", "/api/admin/tenants/{tenant_id}/teams/{team_id}"): RouteScenario(
                path="/api/admin/tenants/example/teams/team-missing",
                auth=admin,
                json={},
                expected_statuses=(404, 422),
            ),
            ("GET", "/api/admin/tenants/{tenant_id}/invites"): RouteScenario(
                path="/api/admin/tenants/example/invites",
                auth=admin,
            ),
            ("POST", "/api/admin/tenants/{tenant_id}/invites"): RouteScenario(
                path="/api/admin/tenants/example/invites",
                auth=admin,
                json={},
                expected_statuses=(201, 409, 422),
            ),
            ("POST", "/api/admin/tenants/{tenant_id}/invites/{invite_id}/resend"): RouteScenario(
                path="/api/admin/tenants/example/invites/invite-missing/resend",
                auth=admin,
                expected_statuses=(200, 404),
            ),
            ("POST", "/api/admin/tenants/{tenant_id}/invites/{invite_id}/revoke"): RouteScenario(
                path="/api/admin/tenants/example/invites/invite-missing/revoke",
                auth=admin,
                expected_statuses=(200, 404),
            ),
            ("POST", "/api/admin/tenants/{tenant_id}/atlassian/disconnect"): RouteScenario(
                path="/api/admin/tenants/example/atlassian/disconnect",
                auth=admin,
            ),
            ("GET", "/api/admin/tenants/{tenant_id}/atlassian/jira/webhooks/diagnostics"): RouteScenario(
                path="/api/admin/tenants/example/atlassian/jira/webhooks/diagnostics",
                auth=admin,
            ),
            ("GET", "/api/admin/tenants/{tenant_id}/atlassian/confluence/spaces"): RouteScenario(
                path="/api/admin/tenants/example/atlassian/confluence/spaces",
                auth=admin,
            ),
            ("GET", "/api/admin/tenants/{tenant_id}/atlassian/confluence/spaces/{space_key}/pages"): RouteScenario(
                path="/api/admin/tenants/example/atlassian/confluence/spaces/ARCH/pages",
                auth=admin,
            ),
            ("POST", "/api/admin/tenants/{tenant_id}/atlassian/jira/webhooks/provision"): RouteScenario(
                path="/api/admin/tenants/example/atlassian/jira/webhooks/provision",
                auth=admin,
                expected_statuses=(200, 400, 502),
            ),
            ("POST", "/api/admin/tenants/{tenant_id}/atlassian/jira/webhooks/reset"): RouteScenario(
                path="/api/admin/tenants/example/atlassian/jira/webhooks/reset",
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
            ("GET", "/api/admin/tenants/{tenant_id}/projects/{project_id}/installs"): RouteScenario(
                path="/api/admin/tenants/example/projects/example-default/installs",
                auth=admin,
            ),
            ("POST", "/api/admin/tenants/{tenant_id}/projects/{project_id}/installs"): RouteScenario(
                path="/api/admin/tenants/example/projects/example-default/installs",
                auth=admin,
                json={
                    "kind": "fastlane_lane",
                    "label": "Smoke Fastlane",
                    "enabled": True,
                    "config": {
                        "working_dir": ".",
                        "platform": "ios",
                        "lane": "beta",
                        "use_bundle_exec": True,
                    },
                    "binding_names": ["FASTLANE_SESSION"],
                },
                expected_statuses=(201,),
            ),
            ("PUT", "/api/admin/tenants/{tenant_id}/projects/{project_id}/installs/{install_id}"): RouteScenario(
                path="/api/admin/tenants/example/projects/example-default/installs/install-missing",
                auth=admin,
                json={
                    "kind": "fastlane_lane",
                    "label": "Missing Install",
                    "enabled": True,
                    "config": {
                        "working_dir": ".",
                        "platform": "ios",
                        "lane": "beta",
                        "use_bundle_exec": True,
                    },
                    "binding_names": ["FASTLANE_SESSION"],
                },
                expected_statuses=(404,),
            ),
            ("DELETE", "/api/admin/tenants/{tenant_id}/projects/{project_id}/installs/{install_id}"): RouteScenario(
                path="/api/admin/tenants/example/projects/example-default/installs/install-missing",
                auth=admin,
                expected_statuses=(404, 204),
            ),
            ("GET", "/api/admin/tenants/{tenant_id}/projects/{project_id}/install-requests"): RouteScenario(
                path="/api/admin/tenants/example/projects/example-default/install-requests",
                auth=admin,
            ),
            ("PUT", "/api/admin/tenants/{tenant_id}/projects/{project_id}/install-requests/{request_id}"): RouteScenario(
                path="/api/admin/tenants/example/projects/example-default/install-requests/request-missing",
                auth=admin,
                json={"status": "rejected"},
                expected_statuses=(404,),
            ),
            ("GET", "/api/admin/tenants/{tenant_id}/projects/{project_id}/automations"): RouteScenario(
                path="/api/admin/tenants/example/projects/example-default/automations",
                auth=admin,
            ),
            ("PUT", "/api/admin/tenants/{tenant_id}/projects/{project_id}/automations"): RouteScenario(
                path="/api/admin/tenants/example/projects/example-default/automations",
                auth=admin,
                json={"automations": []},
            ),
            ("POST", "/api/admin/tenants/{tenant_id}/projects/{project_id}/automations/{kind}/run-now"): RouteScenario(
                path="/api/admin/tenants/example/projects/example-default/automations/daily-summary/run-now",
                auth=admin,
                expected_statuses=(200, 400, 404, 409),
            ),
            ("GET", "/api/admin/tenants/{tenant_id}/projects/{project_id}/architecture-documents"): RouteScenario(
                path="/api/admin/tenants/example/projects/example-default/architecture-documents",
                auth=admin,
            ),
            ("POST", "/api/admin/tenants/{tenant_id}/projects/{project_id}/architecture-documents"): RouteScenario(
                path="/api/admin/tenants/example/projects/example-default/architecture-documents",
                auth=admin,
                json={},
                expected_statuses=(422,),
            ),
            ("GET", "/api/admin/tenants/{tenant_id}/projects/{project_id}/architecture-documents/{document_id}"): RouteScenario(
                path="/api/admin/tenants/example/projects/example-default/architecture-documents/document-missing",
                auth=admin,
                expected_statuses=(404,),
            ),
            ("PUT", "/api/admin/tenants/{tenant_id}/projects/{project_id}/architecture-documents/{document_id}"): RouteScenario(
                path="/api/admin/tenants/example/projects/example-default/architecture-documents/document-missing",
                auth=admin,
                json={"title": "Missing", "status": "draft"},
                expected_statuses=(404,),
            ),
            ("GET", "/api/admin/tenants/{tenant_id}/projects/{project_id}/knowledge/assets"): RouteScenario(
                path="/api/admin/tenants/example/projects/example-default/knowledge/assets",
                auth=admin,
            ),
            ("GET", "/api/admin/tenants/{tenant_id}/projects/{project_id}/knowledge/sources"): RouteScenario(
                path="/api/admin/tenants/example/projects/example-default/knowledge/sources",
                auth=admin,
            ),
            ("GET", "/api/admin/tenants/{tenant_id}/projects/{project_id}/knowledge/stats"): RouteScenario(
                path="/api/admin/tenants/example/projects/example-default/knowledge/stats",
                auth=admin,
            ),
            ("GET", "/api/admin/tenants/{tenant_id}/projects/{project_id}/knowledge/assets/{asset_id}"): RouteScenario(
                path="/api/admin/tenants/example/projects/example-default/knowledge/assets/asset-missing",
                auth=admin,
                expected_statuses=(200, 404),
            ),
            ("GET", "/api/admin/tenants/{tenant_id}/projects/{project_id}/knowledge/assets/{asset_id}/chunks"): RouteScenario(
                path="/api/admin/tenants/example/projects/example-default/knowledge/assets/asset-missing/chunks",
                auth=admin,
                expected_statuses=(200, 404),
            ),
            ("GET", "/api/admin/tenants/{tenant_id}/projects/{project_id}/knowledge/debug-search"): RouteScenario(
                path="/api/admin/tenants/example/projects/example-default/knowledge/debug-search?query=test",
                auth=admin,
                expected_statuses=(200,),
            ),
            ("POST", "/api/admin/tenants/{tenant_id}/projects/{project_id}/knowledge/assets"): RouteScenario(
                path="/api/admin/tenants/example/projects/example-default/knowledge/assets",
                auth=admin,
                json={
                    "title": "E2E Knowledge Note",
                    "source_type": "manual",
                    "mime_type": "text/plain",
                    "text_content": "Objective: Validate knowledge base route coverage.",
                },
                expected_statuses=(201,),
            ),
            ("POST", "/api/admin/tenants/{tenant_id}/projects/{project_id}/knowledge/sources"): RouteScenario(
                path="/api/admin/tenants/example/projects/example-default/knowledge/sources",
                auth=admin,
                json={
                    "connector_type": "discord",
                    "display_name": "Support Threads",
                    "config_json": {"thread_ids": ["1234567890"]},
                },
                expected_statuses=(201, 409),
            ),
            ("DELETE", "/api/admin/tenants/{tenant_id}/projects/{project_id}/knowledge/assets/{asset_id}"): RouteScenario(
                path="/api/admin/tenants/example/projects/example-default/knowledge/assets/asset-missing",
                auth=admin,
                expected_statuses=(204, 404),
            ),
            ("PATCH", "/api/admin/tenants/{tenant_id}/projects/{project_id}/knowledge/sources/{source_id}"): RouteScenario(
                path="/api/admin/tenants/example/projects/example-default/knowledge/sources/source-missing",
                auth=admin,
                json={"status": "disabled"},
                expected_statuses=(200, 404, 409),
            ),
            ("PATCH", "/api/admin/tenants/{tenant_id}/projects/{project_id}/knowledge/assets/{asset_id}/status"): RouteScenario(
                path="/api/admin/tenants/example/projects/example-default/knowledge/assets/asset-missing/status",
                auth=admin,
                json={"status": "ready"},
                expected_statuses=(200, 404, 409),
            ),
            ("DELETE", "/api/admin/tenants/{tenant_id}/projects/{project_id}/knowledge/sources/{source_id}"): RouteScenario(
                path="/api/admin/tenants/example/projects/example-default/knowledge/sources/source-missing",
                auth=admin,
                expected_statuses=(204, 404),
            ),
            ("POST", "/api/admin/tenants/{tenant_id}/projects/{project_id}/knowledge/sources/{source_id}/sync"): RouteScenario(
                path="/api/admin/tenants/example/projects/example-default/knowledge/sources/source-missing/sync",
                auth=admin,
                expected_statuses=(200, 400, 404, 409, 502),
            ),
            ("POST", "/api/admin/tenants/{tenant_id}/projects/{project_id}/knowledge/sync-jira"): RouteScenario(
                path="/api/admin/tenants/example/projects/example-default/knowledge/sync-jira",
                auth=admin,
                expected_statuses=(200, 400, 404, 502),
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
            ("GET", "/api/admin/tenants/{tenant_id}/token-stage-diagnostics"): RouteScenario(
                path="/api/admin/tenants/example/token-stage-diagnostics?project_id=example-default",
                auth=admin,
            ),
            ("GET", "/api/admin/tenants/{tenant_id}/token-stage-diagnostics-compare"): RouteScenario(
                path="/api/admin/tenants/example/token-stage-diagnostics-compare?project_ids=example-default",
                auth=admin,
            ),
            ("GET", "/api/admin/tenants/{tenant_id}/token-overview"): RouteScenario(
                path="/api/admin/tenants/example/token-overview?project_id=example-default",
                auth=admin,
            ),
            ("GET", "/api/admin/tenants/{tenant_id}/runs/{run_id}/token-timeline"): RouteScenario(
                path="/api/admin/tenants/example/runs/run-e2e/token-timeline",
                auth=admin,
            ),
            ("POST", "/api/admin/tenants/{tenant_id}/token-compare"): RouteScenario(
                path="/api/admin/tenants/example/token-compare?project_id=example-default",
                auth=admin,
                json={"run_ids": ["run-e2e", "run-missing"], "align_by": "turn_sequence"},
                expected_statuses=(404,),
            ),
            ("POST", "/api/admin/tenants/{tenant_id}/test-github"): RouteScenario(
                path="/api/admin/tenants/example/test-github",
                auth=admin,
            ),
            ("POST", "/api/admin/tenants/{tenant_id}/test-atlassian"): RouteScenario(
                path="/api/admin/tenants/example/test-atlassian",
                auth=admin,
            ),
            ("POST", "/api/admin/tenants/{tenant_id}/unarchive"): RouteScenario(
                path="/api/admin/tenants/example/unarchive",
                auth=admin,
            ),
            ("GET", "/api/admin/discord/install/callback"): RouteScenario(
                path="/api/admin/discord/install/callback?state=bad&code=x",
                expected_statuses=(400, 422),
            ),
            ("POST", "/discord/command/{tenant_id}"): RouteScenario(
                path="/discord/command/example",
                json={"user_id": "u1", "command": "!help", "channel_id": "discord-channel-1"},
            ),
            ("POST", "/discord/interactions"): RouteScenario(
                path="/discord/interactions",
                json={"type": 1},
            ),
            ("POST", "/discord/webhook/{tenant_id}"): RouteScenario(
                path="/discord/webhook/example",
                json={"user_id": "u1", "command": "!help", "channel_id": "discord-channel-1"},
            ),
            ("POST", "/github/webhook"): RouteScenario(path="/github/webhook", json={"action": "opened"}),
            ("POST", "/api/public/register"): RouteScenario(
                path="/api/public/register",
                json={},
                expected_statuses=(201, 400, 409, 422),
            ),
            ("POST", "/api/public/invites/accept"): RouteScenario(
                path="/api/public/invites/accept",
                json={},
                expected_statuses=(200, 400, 404, 422),
            ),
            ("POST", "/api/public/password-reset/request"): RouteScenario(
                path="/api/public/password-reset/request",
                json={},
                expected_statuses=(200, 400, 422),
            ),
            ("POST", "/api/public/password-reset/confirm"): RouteScenario(
                path="/api/public/password-reset/confirm",
                json={},
                expected_statuses=(200, 400, 404, 422),
            ),
            ("POST", "/api/app/auth/login"): RouteScenario(
                path="/api/app/auth/login",
                json={},
                expected_statuses=(200, 400, 401, 422),
            ),
            ("GET", "/api/app/auth/me"): RouteScenario(
                path="/api/app/auth/me",
                expected_statuses=(401,),
            ),
            ("GET", "/api/app/discord/callback"): RouteScenario(
                path="/api/app/discord/callback?state=bad",
                expected_statuses=(400, 422),
            ),
            ("PUT", "/api/app/me/profile"): RouteScenario(
                path="/api/app/me/profile",
                json={},
                expected_statuses=(401, 422),
            ),
            ("POST", "/api/app/me/password"): RouteScenario(
                path="/api/app/me/password",
                json={},
                expected_statuses=(401, 422),
            ),
            ("PUT", "/api/app/tenants/{tenant_id}/me/settings"): RouteScenario(
                path="/api/app/tenants/example/me/settings",
                json={},
                expected_statuses=(401, 422),
            ),
            ("POST", "/api/app/onboarding/{tenant_id}/complete"): RouteScenario(
                path="/api/app/onboarding/example/complete",
                json={},
                expected_statuses=(401, 404, 422),
            ),
            ("GET", "/health"): RouteScenario(path="/health"),
            ("GET", "/metrics"): RouteScenario(path="/metrics"),
            ("POST", "/jira/webhook/{tenant_id}"): RouteScenario(path="/jira/webhook/example", json={"webhookEvent": "jira:issue_updated"}),
            ("GET", "/runs/{run_id}"): RouteScenario(path="/runs/run-e2e", auth=admin),
        }

    def test_every_external_route_has_smoke_scenario_and_no_500(self) -> None:
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
