import os
import unittest
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, quote_plus, urlparse
from unittest.mock import patch
from types import SimpleNamespace

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select

from orchestrator.api.main import create_app
from orchestrator.api.admin.tenant_crud import purge_expired_archived_tenants
from orchestrator.api.admin.project_normalization import resolve_project_discord_channel_name
from orchestrator.core.config import get_settings
from orchestrator.core.agent_observability import (
    record_agent_lifecycle_event,
    reset_agent_observability_for_tests,
)
from orchestrator.core.run_logs import record_run_log_event
from orchestrator.core.secrets import encrypt_value
from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.models import (
    DiscordCommandSyncRuntimeState,
    JiraOAuthConnection,
    KnowledgeAsset,
    KnowledgeChunk,
    KnowledgeFact,
    KnowledgeJiraSyncRuntimeState,
    KnowledgeSource,
    ManagedSecret,
    PlatformSetting,
    Project,
    ProjectAutomation,
    ProjectInstallRequest,
    Run,
    RunHumanInputRequest,
    Tenant,
    TenantInvite,
    TenantMembership,
    TenantRunClaim,
    TenantTeam,
    TenantTeamMembership,
    TenantUser,
    TenantUserCredential,
    TenantUserDiscordIdentity,
    WebhookJob,
    WorkflowExecution,
    WorkerRuntimeState,
)
from orchestrator.tools.github_app import InstallationRepository
from tests.test_support.db_harness import SqliteTemplateApiTestCase
from tests.workflow_test_support import add_human_input_request, add_run_with_workflow, add_workflow_attempt, make_run


class AdminApiTests(SqliteTemplateApiTestCase):
    _secrets_encryption_key: str

    @classmethod
    def setUpClass(cls) -> None:
        cls._secrets_encryption_key = Fernet.generate_key().decode("utf-8")
        super().setUpClass()

    @classmethod
    def class_environment_overrides(cls) -> dict[str, str]:
        return {
            "ORCHESTRATOR_ADMIN_USERNAME": "admin",
            "ORCHESTRATOR_ADMIN_PASSWORD": "secret",
            "ORCHESTRATOR_ADMIN_TOKEN_SECRET": "admin-token-secret-for-tests-0123456789",
            "ORCHESTRATOR_GITHUB_INSTALL_STATE_SECRET": "unit-test-secret",
            "ORCHESTRATOR_ADMIN_UI_BASE_URL": "http://localhost:4100",
            "ORCHESTRATOR_PUBLIC_API_BASE_URL": "http://localhost:4000",
            "ORCHESTRATOR_JIRA_OAUTH_STATE_SECRET": "jira-oauth-state-secret",
            "ORCHESTRATOR_GITHUB_APP_SLUG": "master-builder-app",
            "ORCHESTRATOR_SECRETS_ENCRYPTION_KEY": cls._secrets_encryption_key,
            "ORCHESTRATOR_CODEX_MODEL": "gpt-5.4",
            "ORCHESTRATOR_CODEX_SUPPORTED_MODELS": "gpt-5.4,gpt-5.3-codex,gpt-5.3-codex-spark",
        }

    @classmethod
    def bootstrap_template_state(cls) -> None:
        seed_slug_secret_response = cls._class_client.put(
            "/api/admin/secrets/platform%2FGITHUB_APP_SLUG",
            json={"value": "master-builder-app"},
            auth=("admin", "secret"),
        )
        if seed_slug_secret_response.status_code != 200:
            raise RuntimeError(
                f"Failed to seed GITHUB_APP_SLUG secret for tests: {seed_slug_secret_response.text}"
            )

        cls._class_client.put(
            "/api/admin/secrets/platform%2FGITHUB_APP_ID",
            json={"value": "12345"},
            auth=("admin", "secret"),
        )
        cls._class_client.put(
            "/api/admin/secrets/platform%2FGITHUB_APP_PRIVATE_KEY",
            json={"value": "not-a-real-key-for-tests"},
            auth=("admin", "secret"),
        )
        cls._class_client.put(
            "/api/admin/secrets/platform%2FJIRA_OAUTH_CLIENT_ID",
            json={"value": "jira-client-id"},
            auth=("admin", "secret"),
        )
        cls._class_client.put(
            "/api/admin/secrets/platform%2FJIRA_OAUTH_CLIENT_SECRET",
            json={"value": "jira-client-secret"},
            auth=("admin", "secret"),
        )

    def setUp(self) -> None:
        self.database_url = self._start_test_database(name_prefix="admin-api")

        get_settings.cache_clear()
        reset_db_engine_cache()
        reset_agent_observability_for_tests()

    def tearDown(self) -> None:
        self._cleanup_test_database()
        os.environ.pop("ORCHESTRATOR_WORKER_CAPABILITIES", None)

        get_settings.cache_clear()
        reset_db_engine_cache()
        reset_agent_observability_for_tests()

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
                "allow_code_reviews": True,
                "allow_pr_remediation": True,
                "allow_manual_pr_fix_requests": True,
                "allow_label_mutations": True,
                "max_runtime_minutes": 30,
                "max_dev_test_review_loops": 2,
                "max_concurrent_runs": 2,
                "allowed_commands": ["python -m unittest"],
                "require_agents_md": False,
                "codex_model": "gpt-5.4",
                "codex_reasoning_effort": "medium",
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

    def _seed_workflow_attempt(
        self,
        *,
        workflow_id: str,
        run_id: str,
        tenant_id: str = "tenant-a",
        project_id: str = "tenant-a-default",
        issue_key: str = "TP-1",
        issue_summary: str = "workflow attempt",
        issue_description: str | None = "desc",
        workflow_status: str = "queued",
        run_status: str = "queued",
        checkpoint_id: str | None = None,
        checkpoint_kind: str | None = None,
        checkpoint_stage: str | None = None,
        pending_request_id: str | None = None,
    ) -> None:
        session_factory = create_session_factory(self.database_url)
        now = datetime.now(timezone.utc)
        with session_factory() as session:
            add_workflow_attempt(
                session,
                workflow_id=workflow_id,
                run_id=run_id,
                tenant_id=tenant_id,
                project_id=project_id,
                issue_key=issue_key,
                issue_summary=issue_summary,
                issue_description=issue_description,
                repo_url="https://github.com/example/repo",
                branch="feature/test",
                workflow_status=workflow_status,
                run_status=run_status,
                entry_checkpoint_id=checkpoint_id,
                checkpoint_kind=checkpoint_kind,
                checkpoint_stage=checkpoint_stage or ("pm" if checkpoint_kind == "pm" else "test"),
                checkpoint_payload={"checkpoint": checkpoint_kind, "run_id": run_id},
                checkpoint_session_id="checkpoint-session" if checkpoint_kind == "pm" else None,
                blocked_reason="human_input_expired" if workflow_status == "blocked" else None,
                last_error=None if workflow_status != "failed" and run_status not in {"failed", "blocked"} else "run failed",
                plan={"source": "test"} if checkpoint_id else None,
                now=now,
            )
            if pending_request_id:
                request_status = "pending" if workflow_status == "waiting_for_input" else "answered"
                add_human_input_request(
                    session,
                    request_id=pending_request_id,
                    tenant_id=tenant_id,
                    project_id=project_id,
                    workflow_id=workflow_id,
                    checkpoint_id=checkpoint_id or "checkpoint-missing",
                    source_run_id=run_id,
                    issue_key=issue_key,
                    source_stage="pm",
                    request_type="human_reply",
                    status=request_status,
                    now=now,
                )
            session.commit()

    def _persist_run(self, session, *, workflow_status: str | None = None, **run_kwargs) -> Run:
        run = make_run(**run_kwargs)
        add_run_with_workflow(session, run, workflow_status=workflow_status)
        return run

    def test_admin_routes_require_auth(self) -> None:
        response = self.client.get("/api/admin/tenants")
        self.assertEqual(response.status_code, 401)

    def test_list_codex_models(self) -> None:
        response = self.client.get("/api/admin/codex/models", auth=("admin", "secret"))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["default_model"], "gpt-5.4")
        self.assertEqual(body["default_reasoning_effort"], "medium")
        self.assertEqual(body["runtime_kind"], "codex_cli")
        self.assertEqual([item["id"] for item in body["models"]], ["gpt-5.4", "gpt-5.3-codex", "gpt-5.3-codex-spark"])
        self.assertEqual([item["id"] for item in body["reasoning_efforts"]], ["medium", "low", "high"])

    def test_list_codex_models_for_engineering_profile_uses_profile_runtime(self) -> None:
        self.client.post(
            "/api/admin/agent-runtime-profiles",
            json={
                "profile_name": "engineering_execution_custom",
                "runtime_kind": "claude_cli",
                "cli_command": "claude",
                "model": "claude-sonnet-4-0",
                "reasoning_effort": "medium",
                "tool_bridge_allowed": True,
                "fallback_profile": "engineering_execution_default",
                "base_url": None,
                "api_key_secret_ref": None,
            },
            auth=("admin", "secret"),
        )
        update_response = self.client.put(
            "/api/admin/agent-runtime-profiles/engineering_execution",
            json={
                "runtime_kind": "claude_cli",
                "cli_command": "claude",
                "model": "claude-sonnet-4-0",
                "reasoning_effort": "medium",
                "tool_bridge_allowed": True,
                "fallback_profile": None,
                "base_url": None,
                "api_key_secret_ref": None,
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(update_response.status_code, 200)

        response = self.client.get(
            "/api/admin/codex/models?profile_name=engineering_execution",
            auth=("admin", "secret"),
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["runtime_kind"], "claude_cli")
        self.assertEqual(body["profile_name"], "engineering_execution")
        self.assertIn("claude-sonnet-4-0", [item["id"] for item in body["models"]])

    def test_list_codex_models_for_lm_studio_includes_reasoning_efforts(self) -> None:
        response = self.client.get(
            "/api/admin/codex/models?runtime_kind=lm_studio",
            auth=("admin", "secret"),
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["runtime_kind"], "lm_studio")
        self.assertEqual([item["id"] for item in body["reasoning_efforts"]], ["medium", "low", "high"])

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
            "/api/admin/secrets/platform%2Fsecret%2Fgithub-webhook",
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
            json={"secret_ref": "platform/secret/github-webhook"},
            auth=("admin", "secret"),
        )
        self.assertEqual(resolve_response.status_code, 200)
        self.assertTrue(resolve_response.json()["resolved"])
        self.assertEqual(resolve_response.json()["source"], "managed")

    def test_agent_runtime_routes_default_response(self) -> None:
        response = self.client.get("/api/admin/agent-runtimes", auth=("admin", "secret"))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["role_routing"], {})
        self.assertEqual(body["name_routing"], {})
        self.assertEqual(body["selector_routing"], {})
        self.assertIn("pm", body["available_roles"])
        self.assertIn("voice_room_pm", body["available_named_agents"])
        self.assertNotIn("discord.voice_entry_router", body["available_named_agents"])
        self.assertIn("discord.voice_entry_router", body["available_selectors"])
        self.assertIn("workflow.standup_voice_brief", body["available_selectors"])
        self.assertIn("workflow.retro_voice_brief", body["available_selectors"])
        self.assertIn("pm_conversation_fast", body["available_profiles"])
        self.assertEqual(body["effective_defaults"]["role_routing"]["pm"], "pm_conversation_default")
        self.assertEqual(body["effective_defaults"]["name_routing"]["workflow_dev_default"], "engineering_execution_default")
        self.assertEqual(body["effective_defaults"]["selector_routing"]["discord.voice_room_pm"], "pm_conversation")

    def test_agent_runtime_tools_catalog_response(self) -> None:
        response = self.client.get("/api/admin/agent-runtime-tools", auth=("admin", "secret"))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("available_stages", body)
        self.assertIn("tools", body)
        self.assertIn("pm", body["available_stages"])
        repo_read = next(item for item in body["tools"] if item["tool_name"] == "repo.read")
        self.assertEqual(repo_read["category"], "repo")
        self.assertIn("dev", repo_read["stages"])
        self.assertIn("pm", repo_read["stages"])
        self.assertTrue(repo_read["description"])

    def test_agent_runtime_profiles_crud_and_reset(self) -> None:
        create_response = self.client.post(
            "/api/admin/agent-runtime-profiles",
            json={
                "profile_name": "openai_engineering_fast",
                "runtime_kind": "openai",
                "cli_command": "",
                "model": "gpt-5.4",
                "reasoning_effort": "low",
                "tool_bridge_allowed": True,
                "fallback_profile": "engineering_execution_default",
                "base_url": "https://api.openai.com/v1",
                "api_key_secret_ref": "platform/openai_api_key",
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(create_response.status_code, 200)
        self.assertEqual(create_response.json()["profile_name"], "openai_engineering_fast")
        self.assertEqual(create_response.json()["runtime_kind"], "openai")
        self.assertEqual(create_response.json()["base_url"], "https://api.openai.com/v1")

        list_response = self.client.get("/api/admin/agent-runtime-profiles", auth=("admin", "secret"))
        self.assertEqual(list_response.status_code, 200)
        self.assertIn("openai_engineering_fast", list_response.json()["profiles"])

        update_response = self.client.put(
            "/api/admin/agent-runtime-profiles/engineering_execution_default",
            json={
                "runtime_kind": "claude_cli",
                "cli_command": "claude",
                "model": "claude-sonnet-4-0",
                "reasoning_effort": "medium",
                "tool_bridge_allowed": True,
                "fallback_profile": None,
                "base_url": None,
                "api_key_secret_ref": None,
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(update_response.status_code, 200)
        self.assertTrue(update_response.json()["is_builtin"])
        self.assertTrue(update_response.json()["is_overridden"])
        self.assertEqual(update_response.json()["runtime_kind"], "claude_cli")

        reset_response = self.client.post(
            "/api/admin/agent-runtime-profiles/engineering_execution_default/reset",
            auth=("admin", "secret"),
        )
        self.assertEqual(reset_response.status_code, 200)
        self.assertEqual(reset_response.json()["runtime_kind"], "codex_cli")
        self.assertFalse(reset_response.json()["is_overridden"])

        delete_response = self.client.delete(
            "/api/admin/agent-runtime-profiles/openai_engineering_fast",
            auth=("admin", "secret"),
        )
        self.assertEqual(delete_response.status_code, 200)
        self.assertNotIn("openai_engineering_fast", delete_response.json()["profiles"])

    def test_agent_runtime_profiles_reject_invalid_provider_configuration(self) -> None:
        response = self.client.post(
            "/api/admin/agent-runtime-profiles",
            json={
                "profile_name": "bad_openai_profile",
                "runtime_kind": "openai",
                "cli_command": "",
                "model": "gpt-5.4",
                "reasoning_effort": "medium",
                "tool_bridge_allowed": True,
                "fallback_profile": None,
                "base_url": None,
                "api_key_secret_ref": None,
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("api_key_secret_ref is required", response.text)

    def test_agent_runtime_profiles_accept_reasoning_for_lm_studio(self) -> None:
        response = self.client.post(
            "/api/admin/agent-runtime-profiles",
            json={
                "profile_name": "lm_studio_reasoning",
                "runtime_kind": "lm_studio",
                "cli_command": "",
                "model": "local-model",
                "reasoning_effort": "high",
                "tool_bridge_allowed": True,
                "fallback_profile": None,
                "base_url": "http://localhost:1234/v1",
                "api_key_secret_ref": None,
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["runtime_kind"], "lm_studio")
        self.assertEqual(response.json()["reasoning_effort"], "high")

    def test_agent_runtime_routes_upsert_and_reset(self) -> None:
        put_response = self.client.put(
            "/api/admin/agent-runtimes",
            json={
                "role_routing": {"pm": "pm_conversation_fast"},
                "name_routing": {"workflow_review_default": "engineering_execution_deep"},
                "selector_routing": {
                    "discord.voice_room_pm": "pm_conversation_fast",
                    "workflow.standup_voice_brief": "general_planning_default",
                    "workflow.retro_voice_brief": "general_planning_default",
                },
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(put_response.status_code, 200)
        body = put_response.json()
        self.assertEqual(body["role_routing"]["pm"], "pm_conversation_fast")
        self.assertEqual(body["name_routing"]["workflow_review_default"], "engineering_execution_deep")
        self.assertEqual(body["selector_routing"]["discord.voice_room_pm"], "pm_conversation_fast")
        self.assertEqual(body["selector_routing"]["workflow.standup_voice_brief"], "general_planning_default")
        self.assertEqual(body["selector_routing"]["workflow.retro_voice_brief"], "general_planning_default")

        get_response = self.client.get("/api/admin/agent-runtimes", auth=("admin", "secret"))
        self.assertEqual(get_response.status_code, 200)
        self.assertEqual(get_response.json()["role_routing"]["pm"], "pm_conversation_fast")
        self.assertEqual(get_response.json()["selector_routing"]["discord.voice_room_pm"], "pm_conversation_fast")
        self.assertEqual(
            get_response.json()["selector_routing"]["workflow.standup_voice_brief"],
            "general_planning_default",
        )
        self.assertEqual(
            get_response.json()["selector_routing"]["workflow.retro_voice_brief"],
            "general_planning_default",
        )

        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            row = session.get(PlatformSetting, "agent_runtime_routing")
            self.assertIsNotNone(row)
            self.assertEqual(row.value_json["role_routing"]["pm"], "pm_conversation_fast")
            self.assertEqual(row.value_json["name_routing"]["workflow_review_default"], "engineering_execution_deep")
            self.assertEqual(row.value_json["selector_routing"]["discord.voice_room_pm"], "pm_conversation_fast")
            self.assertEqual(
                row.value_json["selector_routing"]["workflow.standup_voice_brief"],
                "general_planning_default",
            )
            self.assertEqual(
                row.value_json["selector_routing"]["workflow.retro_voice_brief"],
                "general_planning_default",
            )

        reset_response = self.client.post("/api/admin/agent-runtimes/reset", auth=("admin", "secret"))
        self.assertEqual(reset_response.status_code, 200)
        self.assertEqual(reset_response.json()["role_routing"], {})
        self.assertEqual(reset_response.json()["name_routing"], {})
        self.assertEqual(reset_response.json()["selector_routing"], {})

    def test_agent_runtime_routes_reject_unknown_role_and_profile(self) -> None:
        response = self.client.put(
            "/api/admin/agent-runtimes",
            json={
                "role_routing": {"unknown-role": "pm_conversation_fast"},
                "name_routing": {"workflow_review_default": "missing-profile"},
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Unknown agent role", response.text)

    def test_agent_runtime_routes_reject_unknown_selector(self) -> None:
        response = self.client.put(
            "/api/admin/agent-runtimes",
            json={
                "selector_routing": {"unknown-selector": "pm_conversation_fast"},
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Unknown selector", response.text)

    def test_agent_runtime_routes_normalize_legacy_voice_router_selector(self) -> None:
        response = self.client.put(
            "/api/admin/agent-runtimes",
            json={
                "selector_routing": {"discord.voice_room_router": "general_planning_default"},
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["selector_routing"]["discord.voice_entry_router"],
            "general_planning_default",
        )
        self.assertNotIn("discord.voice_room_router", response.json()["selector_routing"])

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
            "/api/admin/secrets/platform%2Ftemporary-secret",
            json={"value": "temp-value"},
            auth=("admin", "secret"),
        )
        self.assertEqual(put_response.status_code, 200)

        delete_response = self.client.delete(
            "/api/admin/secrets/platform%2Ftemporary-secret",
            auth=("admin", "secret"),
        )
        self.assertEqual(delete_response.status_code, 204)

        resolve_response = self.client.post(
            "/api/admin/secrets/resolve",
            json={"secret_ref": "platform/temporary-secret"},
            auth=("admin", "secret"),
        )
        self.assertEqual(resolve_response.status_code, 200)
        self.assertFalse(resolve_response.json()["resolved"])
        self.assertEqual(resolve_response.json()["source"], "missing")

        missing_delete_response = self.client.delete(
            "/api/admin/secrets/platform%2Ftemporary-secret",
            auth=("admin", "secret"),
        )
        self.assertEqual(missing_delete_response.status_code, 404)

    def test_jira_connect_uses_managed_secret_when_env_not_set(self) -> None:
        os.environ.pop("JIRA_OAUTH_CLIENT_ID", None)
        os.environ.pop("JIRA_OAUTH_CLIENT_SECRET", None)

        self.client.put(
            "/api/admin/secrets/platform%2FJIRA_OAUTH_CLIENT_ID",
            json={"value": "jira-client-id-managed"},
            auth=("admin", "secret"),
        )
        self.client.put(
            "/api/admin/secrets/platform%2FJIRA_OAUTH_CLIENT_SECRET",
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
            claim_row = session.get(TenantRunClaim, "tenant-a")
            self.assertIsNotNone(claim_row)
            self.assertEqual(claim_row.tenant_id, "tenant-a")

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
            patch("orchestrator.api.admin.integration_dependencies.refresh_jira_connection_tokens", return_value="access-token"),
            patch("orchestrator.api.admin.integration_dependencies.jira_oauth_client", return_value=_FakeJiraClient()),
            patch("orchestrator.api.admin.integration_dependencies.github_client_from_tenant_config", return_value=_FakeGitHubClient()),
        ):
            jira_test = self.client.post(
                "/api/admin/tenants/tenant-a/test-jira",
                auth=("admin", "secret"),
            )
        self.assertEqual(jira_test.status_code, 200)
        self.assertTrue(jira_test.json()["ok"])

        with patch("orchestrator.api.admin.integration_dependencies.github_client_from_tenant_config", return_value=_FakeGitHubClient()):
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

    def test_create_tenant_auto_provisions_jira_webhook_when_jira_is_configured(self) -> None:
        payload = self._tenant_payload()
        payload["setup_state"] = {"auto_provision_jira_webhook_on_create": True}
        self._insert_jira_connection(connection_id="conn-1")

        def _fake_provision_jira_webhook(**kwargs: object) -> SimpleNamespace:
            tenant = kwargs["tenant"]
            jira_config = dict(tenant.jira_config)
            jira_config["managed_webhook_ids"] = [2002]
            jira_config["webhook_last_provisioned_at"] = "2026-04-10T14:00:00+00:00"
            jira_config["webhook_last_error"] = None
            tenant.jira_config = jira_config
            return SimpleNamespace(
                ok=True,
                action="provision",
                details="Provisioned 1 Jira webhook(s).",
                webhook_ids=[2002],
            )

        with patch(
            "orchestrator.api.routes.admin_tenants.provision_jira_webhook",
            side_effect=_fake_provision_jira_webhook,
        ) as provision_mock:
            response = self.client.post(
                "/api/admin/tenants",
                json=payload,
                auth=("admin", "secret"),
            )

        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["jira"]["managed_webhook_ids"], [2002])
        self.assertEqual(
            response.json()["jira"]["webhook_last_provisioned_at"],
            "2026-04-10T14:00:00+00:00",
        )
        provision_mock.assert_called_once()
        self.assertFalse(provision_mock.call_args.kwargs["replace_existing"])

    def test_create_tenant_skips_jira_webhook_provision_without_project_keys(self) -> None:
        payload = self._tenant_payload()
        payload["jira"]["project_keys"] = []
        self._insert_jira_connection(connection_id="conn-1")

        with patch("orchestrator.api.routes.admin_tenants.provision_jira_webhook") as provision_mock:
            response = self.client.post(
                "/api/admin/tenants",
                json=payload,
                auth=("admin", "secret"),
            )

        self.assertEqual(response.status_code, 201, response.text)
        provision_mock.assert_not_called()
        self.assertEqual(response.json()["jira"]["managed_webhook_ids"], [])

    def test_create_tenant_rejects_unknown_jira_connection_before_persisting(self) -> None:
        payload = self._tenant_payload()
        payload["setup_state"] = {"auto_provision_jira_webhook_on_create": True}

        response = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )

        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["detail"], "Configured Jira connection was not found")

        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            self.assertIsNone(session.get(Tenant, "tenant-a"))
            self.assertIsNone(session.get(Project, "tenant-a-default"))
            self.assertIsNone(session.get(TenantRunClaim, "tenant-a"))

    def test_create_tenant_rolls_back_when_jira_webhook_provision_fails(self) -> None:
        payload = self._tenant_payload()
        payload["setup_state"] = {"auto_provision_jira_webhook_on_create": True}
        self._insert_jira_connection(connection_id="conn-1")

        with patch(
            "orchestrator.api.routes.admin_tenants.provision_jira_webhook",
            return_value=SimpleNamespace(
                ok=False,
                action="provision",
                details="Failed to provision Jira webhook: missing Jira admin permission",
                webhook_ids=[],
            ),
        ) as provision_mock:
            response = self.client.post(
                "/api/admin/tenants",
                json=payload,
                auth=("admin", "secret"),
            )

        self.assertEqual(response.status_code, 502, response.text)
        self.assertIn("missing Jira admin permission", response.json()["detail"])
        provision_mock.assert_called_once()
        self.assertFalse(provision_mock.call_args.kwargs["replace_existing"])

        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            self.assertIsNone(session.get(Tenant, "tenant-a"))
            self.assertIsNone(session.get(Project, "tenant-a-default"))
            self.assertIsNone(session.get(TenantRunClaim, "tenant-a"))

    def test_update_tenant_preserves_ready_trigger_mode_when_omitted(self) -> None:
        payload = self._tenant_payload()
        payload["jira"]["ready_trigger_mode"] = "transition_only"
        self._insert_jira_connection(connection_id="conn-1")

        create_response = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_response.status_code, 201)
        self.assertEqual(create_response.json()["jira"]["ready_trigger_mode"], "transition_only")

        update_payload = self._tenant_payload()
        update_payload["name"] = "Tenant A Updated"
        update_payload["is_enabled"] = False
        update_payload["jira"].pop("ready_trigger_mode", None)

        update_response = self.client.put(
            "/api/admin/tenants/tenant-a",
            json=update_payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(update_response.status_code, 200)
        self.assertEqual(update_response.json()["jira"]["ready_trigger_mode"], "transition_only")

    def test_update_tenant_drops_live_voice_discord_fields_when_omitted(self) -> None:
        payload = self._tenant_payload()
        payload["discord"]["live_voice_enabled"] = True
        payload["discord"]["live_voice_room_links"] = {"voice-room-1": "text-room-1"}
        self._insert_jira_connection(connection_id="conn-1")

        create_response = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_response.status_code, 201)
        self.assertTrue(create_response.json()["discord"]["live_voice_enabled"])
        self.assertEqual(
            create_response.json()["discord"]["live_voice_room_links"],
            {"voice-room-1": "text-room-1"},
        )

        update_payload = self._tenant_payload()
        update_payload["name"] = "Tenant A Updated"
        update_payload["is_enabled"] = False
        update_payload["discord"].pop("live_voice_enabled", None)
        update_payload["discord"].pop("live_voice_room_links", None)

        update_response = self.client.put(
            "/api/admin/tenants/tenant-a",
            json=update_payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(update_response.status_code, 200)
        self.assertNotIn("live_voice_enabled", update_response.json()["discord"])
        self.assertNotIn("live_voice_room_links", update_response.json()["discord"])

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
            patch("orchestrator.api.admin.integration_dependencies.refresh_jira_connection_tokens", return_value="access-token"),
            patch("orchestrator.api.admin.integration_dependencies.jira_oauth_client", return_value=fake_client),
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
            patch("orchestrator.api.admin.integration_dependencies.refresh_jira_connection_tokens", return_value="access-token"),
            patch("orchestrator.api.admin.integration_dependencies.jira_oauth_client", return_value=_FakeJiraClient()),
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
        self.assertEqual(
            create_project.json()["secret_refs"],
            {"API_TOKEN": "RUNNER_TOKEN"},
        )
        self.assertIsNone(create_project.json()["discord"])
        self.assertEqual(create_project.json()["effective_policy"]["codex_model"], "gpt-5.4")
        self.assertEqual(create_project.json()["effective_policy"]["codex_reasoning_effort"], "medium")

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
                "policy_overrides": {
                    "allow_code_reviews": False,
                    "allow_pr_remediation": False,
                    "allow_manual_pr_fix_requests": False,
                    "allow_auto_merge": False,
                    "max_pr_auto_remediation_loops": 3,
                    "knowledge_base_enabled": False,
                    "knowledge_auto_answer_mode": "safe",
                    "allowed_commands": ["git status"],
                    "codex_model": "gpt-5.3-codex-spark",
                    "codex_reasoning_effort": "high",
                },
                "environment": {"APP_ENV": "stage"},
                "secret_refs": {"API_TOKEN": "RUNNER_TOKEN_NEXT"},
                "is_archived": True,
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(update_project.status_code, 200)
        self.assertTrue(update_project.json()["is_archived"])
        self.assertEqual(update_project.json()["environment"], {"APP_ENV": "stage"})
        self.assertEqual(
            update_project.json()["secret_refs"],
            {"API_TOKEN": "RUNNER_TOKEN_NEXT"},
        )
        self.assertIsNone(update_project.json()["discord"])
        self.assertEqual(update_project.json()["policy_overrides"]["codex_model"], "gpt-5.3-codex-spark")
        self.assertFalse(update_project.json()["policy_overrides"]["allow_code_reviews"])
        self.assertFalse(update_project.json()["policy_overrides"]["allow_pr_remediation"])
        self.assertFalse(update_project.json()["policy_overrides"]["allow_manual_pr_fix_requests"])
        self.assertEqual(update_project.json()["policy_overrides"]["max_pr_auto_remediation_loops"], 3)
        self.assertFalse(update_project.json()["policy_overrides"]["knowledge_base_enabled"])
        self.assertEqual(update_project.json()["policy_overrides"]["knowledge_auto_answer_mode"], "safe")
        self.assertEqual(update_project.json()["policy_overrides"]["allowed_commands"], ["git status"])
        self.assertEqual(update_project.json()["policy_overrides"]["codex_reasoning_effort"], "high")
        self.assertEqual(update_project.json()["effective_policy"]["codex_model"], "gpt-5.3-codex-spark")
        self.assertEqual(update_project.json()["effective_policy"]["codex_reasoning_effort"], "high")
        self.assertFalse(update_project.json()["effective_policy"]["allow_code_reviews"])
        self.assertFalse(update_project.json()["effective_policy"]["allow_pr_remediation"])
        self.assertFalse(update_project.json()["effective_policy"]["allow_manual_pr_fix_requests"])
        self.assertFalse(update_project.json()["effective_policy"]["allow_auto_merge"])
        self.assertEqual(update_project.json()["effective_policy"]["max_pr_auto_remediation_loops"], 3)
        self.assertFalse(update_project.json()["effective_policy"]["knowledge_base_enabled"])
        self.assertEqual(update_project.json()["effective_policy"]["knowledge_auto_answer_mode"], "safe")
        self.assertEqual(update_project.json()["effective_policy"]["allowed_commands"], [])

    def test_project_update_migrates_inline_secret_values_to_project_managed_refs(self) -> None:
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

        update_project = self.client.put(
            f"/api/admin/tenants/tenant-a/projects/{project_id}",
            json={
                "name": default_project["name"],
                "github_repository": default_project["github_repository"],
                "jira_project_key": default_project["jira_project_key"],
                "environment": {},
                "secret_refs": {
                    "SUPABASE_URL": "https://example.supabase.co",
                    "APPLE_TEST_PASSWORD": "Ft6ygA&aYkf%hy",
                },
                "is_archived": False,
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(update_project.status_code, 200)
        body = update_project.json()
        self.assertEqual(
            body["secret_refs"],
            {
                "SUPABASE_URL": f"project/tenant-a/{project_id}/SUPABASE_URL",
                "APPLE_TEST_PASSWORD": f"project/tenant-a/{project_id}/APPLE_TEST_PASSWORD",
            },
        )

        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            project = session.get(Project, project_id)
            assert project is not None
            self.assertEqual(project.secret_refs, body["secret_refs"])
            supabase_secret = session.get(ManagedSecret, f"project/tenant-a/{project_id}/SUPABASE_URL")
            password_secret = session.get(ManagedSecret, f"project/tenant-a/{project_id}/APPLE_TEST_PASSWORD")
            self.assertIsNotNone(supabase_secret)
            self.assertIsNotNone(password_secret)

    def test_project_update_preserves_upstream_secret_refs_without_creating_project_copies(self) -> None:
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
        project_id = projects_response.json()[0]["project_id"]

        update_project = self.client.put(
            f"/api/admin/tenants/tenant-a/projects/{project_id}",
            json={
                "name": projects_response.json()[0]["name"],
                "github_repository": projects_response.json()[0]["github_repository"],
                "jira_project_key": projects_response.json()[0]["jira_project_key"],
                "environment": {},
                "secret_refs": {
                    "RAILWAY_TOKEN": "platform/RAILWAY_TOKEN",
                    "SUPABASE_SERVICE_ROLE_KEY": "tenant/tenant-a/SUPABASE_SERVICE_ROLE_KEY",
                },
                "is_archived": False,
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(update_project.status_code, 200)
        body = update_project.json()
        self.assertEqual(
            body["secret_refs"],
            {
                "RAILWAY_TOKEN": "platform/RAILWAY_TOKEN",
                "SUPABASE_SERVICE_ROLE_KEY": "tenant/tenant-a/SUPABASE_SERVICE_ROLE_KEY",
            },
        )

        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            project = session.get(Project, project_id)
            assert project is not None
            self.assertEqual(project.secret_refs, body["secret_refs"])
            self.assertIsNone(session.get(ManagedSecret, f"project/tenant-a/{project_id}/RAILWAY_TOKEN"))
            self.assertIsNone(session.get(ManagedSecret, f"project/tenant-a/{project_id}/SUPABASE_SERVICE_ROLE_KEY"))

    def test_project_create_auto_provisions_discord_channel_when_tenant_discord_is_installed(self) -> None:
        payload = self._tenant_payload()
        payload["discord"] = None
        self._insert_jira_connection(connection_id="conn-1")
        create_tenant = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_tenant.status_code, 201)

        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            tenant = session.get(Tenant, "tenant-a")
            self.assertIsNotNone(tenant)
            assert tenant is not None
            tenant.discord_config = {"guild_id": "discord-guild-1", "notify_events": []}
            tenant.updated_at = datetime.now(timezone.utc)
            session.commit()

        with patch(
            "orchestrator.api.admin.route_helpers.resolve_project_discord_channel_binding",
            return_value={"channel_id": "discord-project-channel-1"},
        ) as resolve_channel_mock:
            create_project = self.client.post(
                "/api/admin/tenants/tenant-a/projects",
                json={
                    "name": "mobile-app",
                    "github_repository": "https://github.com/example/mobile-app",
                    "jira_project_key": "MBAPP",
                },
                auth=("admin", "secret"),
            )

        self.assertEqual(create_project.status_code, 201, create_project.text)
        self.assertEqual(create_project.json()["discord"], {"channel_id": "discord-project-channel-1"})
        resolve_channel_mock.assert_called_once()

    def test_project_installs_crud_and_request_surfaces_round_trip(self) -> None:
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
        project_id = projects_response.json()[0]["project_id"]

        create_install = self.client.post(
            f"/api/admin/tenants/tenant-a/projects/{project_id}/installs",
            json={
                "kind": "fastlane_lane",
                "label": "iOS Beta Lane",
                "enabled": True,
                "config": {"working_dir": ".", "platform": "ios", "lane": "beta", "use_bundle_exec": True},
                "binding_names": ["MATCH_PASSWORD", "FASTLANE_SESSION"],
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(create_install.status_code, 201)
        install_id = create_install.json()["install_id"]

        list_installs = self.client.get(
            f"/api/admin/tenants/tenant-a/projects/{project_id}/installs",
            auth=("admin", "secret"),
        )
        self.assertEqual(list_installs.status_code, 200)
        self.assertEqual(list_installs.json()["installs"][0]["label"], "iOS Beta Lane")
        self.assertEqual(
            list_installs.json()["installs"][0]["binding_names"],
            ["MATCH_PASSWORD", "FASTLANE_SESSION"],
        )

        update_install = self.client.put(
            f"/api/admin/tenants/tenant-a/projects/{project_id}/installs/{install_id}",
            json={
                "kind": "fastlane_lane",
                "label": "iOS Release Lane",
                "enabled": False,
                "config": {"working_dir": ".", "platform": "ios", "lane": "release", "use_bundle_exec": True},
                "binding_names": ["MATCH_PASSWORD"],
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(update_install.status_code, 200)
        self.assertEqual(update_install.json()["label"], "iOS Release Lane")
        self.assertFalse(update_install.json()["enabled"])

        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            session.add(
                ProjectInstallRequest(
                    request_id="install-request-1",
                    tenant_id="tenant-a",
                    project_id=project_id,
                    workflow_id="workflow-1",
                    run_id="run-1",
                    issue_key="MB-101",
                    kind="fastlane_lane",
                    label="iOS Release Lane",
                    reason="Ticket requires Fastlane release automation",
                    suggested_config_json={"working_dir": ".", "platform": "ios", "lane": "release"},
                    required_bindings_json=["MATCH_PASSWORD"],
                    status="pending",
                    request_kind="project_missing_install",
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )
            )
            session.commit()

        list_requests = self.client.get(
            f"/api/admin/tenants/tenant-a/projects/{project_id}/install-requests",
            auth=("admin", "secret"),
        )
        self.assertEqual(list_requests.status_code, 200)
        self.assertEqual(list_requests.json()["requests"][0]["request_id"], "install-request-1")

        fulfill_request = self.client.put(
            f"/api/admin/tenants/tenant-a/projects/{project_id}/install-requests/install-request-1",
            json={"status": "fulfilled"},
            auth=("admin", "secret"),
        )
        self.assertEqual(fulfill_request.status_code, 200)
        self.assertEqual(fulfill_request.json()["status"], "fulfilled")

        delete_install = self.client.delete(
            f"/api/admin/tenants/tenant-a/projects/{project_id}/installs/{install_id}",
            auth=("admin", "secret"),
        )
        self.assertEqual(delete_install.status_code, 204)

        final_installs = self.client.get(
            f"/api/admin/tenants/tenant-a/projects/{project_id}/installs",
            auth=("admin", "secret"),
        )
        self.assertEqual(final_installs.status_code, 200)
        self.assertEqual(final_installs.json()["installs"], [])

    def test_project_automations_round_trip_and_stays_out_of_discord_config(self) -> None:
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
        project_id = projects_response.json()[0]["project_id"]

        initial_response = self.client.get(
            f"/api/admin/tenants/tenant-a/projects/{project_id}/automations",
            auth=("admin", "secret"),
        )
        self.assertEqual(initial_response.status_code, 200)
        self.assertEqual(initial_response.json()["automations"], [])

        update_response = self.client.put(
            f"/api/admin/tenants/tenant-a/projects/{project_id}/automations",
            json={
                "automations": [
                    {
                        "kind": "standup_voice_brief",
                        "enabled": True,
                        "timezone": "Europe/London",
                        "days_of_week": [0, 1, 2, 3, 4],
                        "local_time": "09:30",
                        "fallback_lookback_hours": 24,
                    }
                ],
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(update_response.status_code, 200)
        self.assertEqual(len(update_response.json()["automations"]), 1)
        self.assertEqual(update_response.json()["automations"][0]["kind"], "standup_voice_brief")

        repeat_response = self.client.get(
            f"/api/admin/tenants/tenant-a/projects/{project_id}/automations",
            auth=("admin", "secret"),
        )
        self.assertEqual(repeat_response.status_code, 200)
        self.assertEqual(len(repeat_response.json()["automations"]), 1)

        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            project = session.get(Project, project_id)
            self.assertIsNotNone(project)
            assert project is not None
            self.assertEqual(project.discord_config, {})
            automation = (
                session.query(ProjectAutomation)
                .filter_by(project_id=project_id, tenant_id="tenant-a")
                .one_or_none()
            )
            self.assertIsNotNone(automation)
            assert automation is not None
            self.assertTrue(automation.enabled)
            self.assertEqual(automation.kind, "standup_voice_brief")

    def test_project_automations_round_trip_without_delivery_channel_field(self) -> None:
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
        project_id = projects_response.json()[0]["project_id"]

        update_response = self.client.put(
            f"/api/admin/tenants/tenant-a/projects/{project_id}/automations",
            json={
                "automations": [
                    {
                        "kind": "standup_voice_brief",
                        "enabled": True,
                        "timezone": "Europe/London",
                        "days_of_week": [0, 1, 2, 3, 4],
                        "local_time": "09:30",
                        "fallback_lookback_hours": 24,
                    }
                ],
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(update_response.status_code, 200)
        body = update_response.json()["automations"][0]
        self.assertNotIn("delivery_text_channel_id", body)

        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            automation = (
                session.query(ProjectAutomation)
                .filter_by(project_id=project_id, tenant_id="tenant-a")
                .one_or_none()
            )
            self.assertIsNotNone(automation)
            assert automation is not None

    def test_project_automations_validate_scoping_and_values(self) -> None:
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
        project_id = projects_response.json()[0]["project_id"]

        second_payload = self._tenant_payload()
        second_payload["name"] = "Tenant B"
        second_payload["jira"]["connection_id"] = "conn-1"
        second_create = self.client.post(
            "/api/admin/tenants",
            json=second_payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(second_create.status_code, 201)

        invalid_payload_response = self.client.put(
            f"/api/admin/tenants/tenant-a/projects/{project_id}/automations",
            json={
                "automations": [
                    {
                        "kind": "invalid_kind",
                        "enabled": True,
                        "timezone": "Europe/London",
                        "days_of_week": [0],
                        "local_time": "09:30",
                        "fallback_lookback_hours": 24,
                    }
                ],
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(invalid_payload_response.status_code, 400)

        scoping_response = self.client.get(
            f"/api/admin/tenants/{second_create.json()['tenant_id']}/projects/{project_id}/automations",
            auth=("admin", "secret"),
        )
        self.assertEqual(scoping_response.status_code, 404)

    def test_project_automations_run_now_queues_execution(self) -> None:
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
        project_id = projects_response.json()[0]["project_id"]

        update_response = self.client.put(
            f"/api/admin/tenants/tenant-a/projects/{project_id}/automations",
            json={
                "automations": [
                    {
                        "kind": "standup_voice_brief",
                        "enabled": True,
                        "timezone": "Europe/London",
                        "days_of_week": [0, 1, 2, 3, 4],
                        "local_time": "09:30",
                        "fallback_lookback_hours": 24,
                    }
                ],
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(update_response.status_code, 200)

        run_now_response = self.client.post(
            f"/api/admin/tenants/tenant-a/projects/{project_id}/automations/standup_voice_brief/run-now",
            auth=("admin", "secret"),
        )
        self.assertEqual(run_now_response.status_code, 200)
        body = run_now_response.json()
        self.assertEqual(len(body["automations"]), 1)
        self.assertEqual(body["automations"][0]["kind"], "standup_voice_brief")
        self.assertGreaterEqual(len(body["automations"][0]["executions"]), 1)

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
            "orchestrator.api.admin.route_helpers.resolve_project_discord_channel_binding",
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
                "live_voice_enabled": True,
                "live_voice_room_links": {"voice-room-1": "text-room-1"},
            }
            session.add(project)
            session.commit()

        with patch(
            "orchestrator.api.admin.route_helpers.resolve_project_discord_channel_binding",
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
        self.assertNotIn("live_voice_enabled", response.json()["discord"])
        self.assertNotIn("live_voice_room_links", response.json()["discord"])

        with session_factory() as session:
            project = session.get(Project, project_id)
            assert project is not None
            discord_config = dict(project.discord_config or {})
            self.assertEqual(discord_config.get("allowed_user_ids"), ["discord-user-1"])
            self.assertEqual(len(discord_config.get("allowlist_requests", [])), 1)
            self.assertNotIn("live_voice_enabled", discord_config)
            self.assertNotIn("live_voice_room_links", discord_config)

    def test_update_project_persists_live_voice_room_links(self) -> None:
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
            "orchestrator.api.admin.route_helpers.resolve_project_discord_channel_binding",
            side_effect=lambda **kwargs: dict(kwargs["discord_config"]),
        ):
            response = self.client.put(
                f"/api/admin/tenants/tenant-a/projects/{project_id}",
                json={
                    "name": default_project["name"],
                    "github_repository": default_project["github_repository"],
                    "jira_project_key": default_project["jira_project_key"],
                    "discord": {
                        "notify_events": ["run_failed"],
                        "live_voice_enabled": True,
                        "live_voice_room_links": {
                            "voice-room-9": "text-room-9",
                            "voice-room-10": "text-room-10",
                        },
                    },
                    "is_archived": False,
                },
                auth=("admin", "secret"),
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["discord"]["live_voice_enabled"])
        self.assertEqual(
            response.json()["discord"]["live_voice_room_links"],
            {
                "voice-room-9": "text-room-9",
                "voice-room-10": "text-room-10",
            },
        )

    def test_update_project_can_clear_live_voice_room_links(self) -> None:
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
            "orchestrator.api.admin.route_helpers.resolve_project_discord_channel_binding",
            side_effect=lambda **kwargs: dict(kwargs["discord_config"]),
        ):
            response = self.client.put(
                f"/api/admin/tenants/tenant-a/projects/{project_id}",
                json={
                    "name": default_project["name"],
                    "github_repository": default_project["github_repository"],
                    "jira_project_key": default_project["jira_project_key"],
                    "discord": {
                        "notify_events": ["run_failed"],
                        "live_voice_enabled": False,
                        "live_voice_room_links": {},
                    },
                    "is_archived": False,
                },
                auth=("admin", "secret"),
            )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["discord"]["live_voice_enabled"])
        self.assertEqual(response.json()["discord"]["live_voice_room_links"], {})

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
        channel_name = resolve_project_discord_channel_name(settings=settings, tenant=tenant, project=project)
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

        self._seed_workflow_attempt(
            workflow_id="workflow-default-project",
            run_id="run-default-project",
            project_id="tenant-a-default",
            issue_key="TP-1",
            issue_summary="Default project run",
            workflow_status="queued",
            run_status="queued",
        )
        self._seed_workflow_attempt(
            workflow_id="workflow-created-project",
            run_id="run-created-project",
            project_id=created_project_id,
            issue_key="MBAPP-2",
            issue_summary="Created project run",
            workflow_status="running",
            run_status="running",
        )

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
        self.assertEqual(filtered_runs[0]["workflow_id"], "workflow-created-project")
        self.assertEqual(filtered_runs[0]["attempt_number"], 1)
        self.assertEqual(filtered_runs[0]["entry_mode"], "fresh")
        self.assertEqual(filtered_runs[0]["entry_stage"], "orchestrated")
        self.assertNotIn("dev_session_id", filtered_runs[0])

    def test_legacy_admin_run_rerun_route_is_removed(self) -> None:
        response = self.client.post(
            "/api/admin/runs/run-missing/rerun",
            json={"mode": "resume", "resume_stage": "dev"},
            auth=("admin", "secret"),
        )
        self.assertEqual(response.status_code, 404)

    def test_list_and_get_workflows_from_admin(self) -> None:
        payload = self._tenant_payload()
        self._insert_jira_connection(connection_id="conn-1")
        create_tenant = self.client.post("/api/admin/tenants", json=payload, auth=("admin", "secret"))
        self.assertEqual(create_tenant.status_code, 201)

        self._seed_workflow_attempt(
            workflow_id="workflow-read-1",
            run_id="run-read-1",
            issue_key="TP-999",
            issue_summary="Workflow read model",
            workflow_status="waiting_for_input",
            run_status="waiting_for_input",
            checkpoint_id="checkpoint-read-1",
            checkpoint_kind="pm",
            pending_request_id="request-read-1",
        )

        list_response = self.client.get("/api/admin/workflows?tenant_id=tenant-a", auth=("admin", "secret"))
        self.assertEqual(list_response.status_code, 200, list_response.text)
        list_body = list_response.json()
        self.assertEqual(len(list_body), 1)
        self.assertEqual(list_body[0]["workflow_id"], "workflow-read-1")
        self.assertEqual(list_body[0]["latest_checkpoint_kind"], "pm")
        self.assertEqual(list_body[0]["pending_input_request_id"], "request-read-1")
        self.assertEqual(list_body[0]["runs"][0]["workflow_id"], "workflow-read-1")
        self.assertNotIn("dev_session_id", list_body[0]["runs"][0])

        detail_response = self.client.get("/api/admin/workflows/workflow-read-1", auth=("admin", "secret"))
        self.assertEqual(detail_response.status_code, 200, detail_response.text)
        detail_body = detail_response.json()
        self.assertEqual(detail_body["status"], "waiting_for_input")
        self.assertEqual(detail_body["runs"][0]["attempt_number"], 1)
        self.assertEqual(detail_body["runs"][0]["entry_checkpoint_id"], "checkpoint-read-1")

    def test_create_workflow_attempt_reuses_waiting_workflow(self) -> None:
        payload = self._tenant_payload()
        self._insert_jira_connection(connection_id="conn-1")
        create_tenant = self.client.post("/api/admin/tenants", json=payload, auth=("admin", "secret"))
        self.assertEqual(create_tenant.status_code, 201)

        self._seed_workflow_attempt(
            workflow_id="workflow-waiting-1",
            run_id="run-waiting-1",
            issue_key="TP-1000",
            issue_summary="Waiting workflow",
            workflow_status="waiting_for_input",
            run_status="waiting_for_input",
            checkpoint_id="checkpoint-waiting-1",
            checkpoint_kind="pm",
            pending_request_id="request-waiting-1",
        )

        response = self.client.post(
            "/api/admin/workflows/workflow-waiting-1/attempts",
            json={"mode": "resume", "checkpoint_kind": "pm"},
            auth=("admin", "secret"),
        )
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(body["workflow_id"], "workflow-waiting-1")
        self.assertEqual(body["attempt_number"], 2)
        self.assertEqual(body["entry_mode"], "resume")
        self.assertEqual(body["entry_stage"], "pm")
        self.assertEqual(body["entry_checkpoint_id"], "checkpoint-waiting-1")

        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            workflow = session.get(WorkflowExecution, "workflow-waiting-1")
            self.assertIsNotNone(workflow)
            assert workflow is not None
            self.assertEqual(workflow.status, "queued")
            self.assertEqual(workflow.active_run_id, body["run_id"])
            request = session.get(RunHumanInputRequest, "request-waiting-1")
            self.assertIsNotNone(request)
            assert request is not None
            self.assertEqual(request.status, "cancelled")

    def test_create_workflow_attempt_rejects_restart_while_waiting_for_input(self) -> None:
        payload = self._tenant_payload()
        self._insert_jira_connection(connection_id="conn-1")
        create_tenant = self.client.post("/api/admin/tenants", json=payload, auth=("admin", "secret"))
        self.assertEqual(create_tenant.status_code, 201)

        self._seed_workflow_attempt(
            workflow_id="workflow-waiting-restart-rejected-1",
            run_id="run-waiting-restart-rejected-1",
            issue_key="TP-1000B",
            issue_summary="Waiting workflow restart rejected",
            workflow_status="waiting_for_input",
            run_status="waiting_for_input",
            checkpoint_id="checkpoint-waiting-restart-rejected-1",
            checkpoint_kind="pm",
            pending_request_id="request-waiting-restart-rejected-1",
        )

        response = self.client.post(
            "/api/admin/workflows/workflow-waiting-restart-rejected-1/attempts",
            json={"mode": "restart", "checkpoint_kind": "pm"},
            auth=("admin", "secret"),
        )
        self.assertEqual(response.status_code, 409, response.text)

    def test_create_workflow_attempt_from_terminal_workflow_creates_new_workflow_lineage(self) -> None:
        payload = self._tenant_payload()
        self._insert_jira_connection(connection_id="conn-1")
        create_tenant = self.client.post("/api/admin/tenants", json=payload, auth=("admin", "secret"))
        self.assertEqual(create_tenant.status_code, 201)

        self._seed_workflow_attempt(
            workflow_id="workflow-terminal-1",
            run_id="run-terminal-1",
            issue_key="TP-1001",
            issue_summary="Terminal workflow",
            workflow_status="failed",
            run_status="failed",
            checkpoint_id="checkpoint-terminal-1",
            checkpoint_kind="execution",
        )

        response = self.client.post(
            "/api/admin/workflows/workflow-terminal-1/attempts",
            json={"mode": "restart", "checkpoint_kind": "execution"},
            auth=("admin", "secret"),
        )
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertNotEqual(body["workflow_id"], "workflow-terminal-1")
        self.assertEqual(body["attempt_number"], 1)
        self.assertEqual(body["entry_mode"], "restart")
        self.assertEqual(body["entry_stage"], "test")
        self.assertEqual(body["entry_checkpoint_id"], "checkpoint-terminal-1")

        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            workflow = session.get(WorkflowExecution, body["workflow_id"])
            self.assertIsNotNone(workflow)
            assert workflow is not None
            self.assertEqual(workflow.source_workflow_id, "workflow-terminal-1")
            self.assertEqual(workflow.source_run_id, "run-terminal-1")
            self.assertEqual(workflow.status, "queued")

    def test_create_workflow_attempt_repairs_stale_active_status_before_policy_check(self) -> None:
        payload = self._tenant_payload()
        self._insert_jira_connection(connection_id="conn-1")
        create_tenant = self.client.post("/api/admin/tenants", json=payload, auth=("admin", "secret"))
        self.assertEqual(create_tenant.status_code, 201)

        self._seed_workflow_attempt(
            workflow_id="workflow-stale-active-1",
            run_id="run-stale-active-1",
            issue_key="TP-1001B",
            issue_summary="Stale active workflow",
            workflow_status="queued",
            run_status="failed",
            checkpoint_id="checkpoint-stale-active-1",
            checkpoint_kind="execution",
        )

        response = self.client.post(
            "/api/admin/workflows/workflow-stale-active-1/attempts",
            json={"mode": "restart", "checkpoint_kind": "execution"},
            auth=("admin", "secret"),
        )
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertNotEqual(body["workflow_id"], "workflow-stale-active-1")
        self.assertEqual(body["entry_mode"], "restart")
        self.assertEqual(body["entry_stage"], "test")

        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            stale = session.get(WorkflowExecution, "workflow-stale-active-1")
            self.assertIsNotNone(stale)
            assert stale is not None
            self.assertEqual(stale.status, "failed")

    def test_create_workflow_attempt_returns_conflict_when_scope_already_has_active_workflow(self) -> None:
        payload = self._tenant_payload()
        self._insert_jira_connection(connection_id="conn-1")
        create_tenant = self.client.post("/api/admin/tenants", json=payload, auth=("admin", "secret"))
        self.assertEqual(create_tenant.status_code, 201)

        self._seed_workflow_attempt(
            workflow_id="workflow-terminal-conflict-1",
            run_id="run-terminal-conflict-1",
            issue_key="TP-1001C",
            issue_summary="Terminal workflow with active-scope conflict",
            workflow_status="failed",
            run_status="failed",
            checkpoint_id="checkpoint-terminal-conflict-1",
            checkpoint_kind="execution",
        )
        self._seed_workflow_attempt(
            workflow_id="workflow-active-conflict-1",
            run_id="run-active-conflict-1",
            issue_key="TP-1001C",
            issue_summary="Active workflow occupying dedupe scope",
            workflow_status="queued",
            run_status="queued",
        )

        response = self.client.post(
            "/api/admin/workflows/workflow-terminal-conflict-1/attempts",
            json={"mode": "restart", "checkpoint_kind": "execution"},
            auth=("admin", "secret"),
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("already exists for this issue and dedupe scope", response.json()["detail"])

    def test_create_fresh_workflow_attempt_from_terminal_workflow_starts_without_checkpoint(self) -> None:
        payload = self._tenant_payload()
        self._insert_jira_connection(connection_id="conn-1")
        create_tenant = self.client.post("/api/admin/tenants", json=payload, auth=("admin", "secret"))
        self.assertEqual(create_tenant.status_code, 201)

        self._seed_workflow_attempt(
            workflow_id="workflow-terminal-fresh-1",
            run_id="run-terminal-fresh-1",
            issue_key="TP-1002",
            issue_summary="Terminal workflow fresh start",
            workflow_status="failed",
            run_status="failed",
            checkpoint_id="checkpoint-terminal-fresh-1",
            checkpoint_kind="execution",
        )

        with create_session_factory(self.database_url)() as session:
            source_run = session.get(Run, "run-terminal-fresh-1")
            assert source_run is not None
            snapshot = ExecutionSnapshot.empty(
                trigger_context={"source": "manual_fix_request", "pr_number": 42}
            )
            snapshot.context.execution_context["pre_check_outcome"] = "ready_for_agent"
            source_run.plan = snapshot.dump()
            session.commit()

        response = self.client.post(
            "/api/admin/workflows/workflow-terminal-fresh-1/attempts",
            json={"mode": "fresh"},
            auth=("admin", "secret"),
        )
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertNotEqual(body["workflow_id"], "workflow-terminal-fresh-1")
        self.assertEqual(body["attempt_number"], 1)
        self.assertEqual(body["entry_mode"], "fresh")
        self.assertEqual(body["entry_stage"], "orchestrated")
        self.assertIsNone(body["entry_checkpoint_id"])
        self.assertIsNone(body["branch"])
        self.assertIsNone(body["pr_url"])

        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            workflow = session.get(WorkflowExecution, body["workflow_id"])
            self.assertIsNotNone(workflow)
            assert workflow is not None
            self.assertEqual(workflow.source_workflow_id, "workflow-terminal-fresh-1")
            self.assertEqual(workflow.source_run_id, "run-terminal-fresh-1")
            self.assertIsNone(workflow.latest_checkpoint_id)
            self.assertIsNone(workflow.branch)
            self.assertIsNone(workflow.pr_url)
            run = session.get(Run, body["run_id"])
            self.assertIsNotNone(run)
            assert run is not None
            assert isinstance(run.plan, dict)
            self.assertEqual(
                run.plan["context"]["trigger_context"],
                {"source": "manual_fix_request", "pr_number": 42},
            )
            self.assertEqual(
                run.plan["context"]["execution_context"]["pre_check_outcome"],
                "ready_for_agent",
            )

    def test_create_fresh_workflow_attempt_from_blocked_workflow_creates_new_workflow_lineage(self) -> None:
        payload = self._tenant_payload()
        self._insert_jira_connection(connection_id="conn-1")
        create_tenant = self.client.post("/api/admin/tenants", json=payload, auth=("admin", "secret"))
        self.assertEqual(create_tenant.status_code, 201)

        self._seed_workflow_attempt(
            workflow_id="workflow-blocked-fresh-1",
            run_id="run-blocked-fresh-1",
            issue_key="TP-1002B",
            issue_summary="Blocked workflow fresh retry",
            workflow_status="blocked",
            run_status="blocked",
            checkpoint_id="checkpoint-blocked-fresh-1",
            checkpoint_kind="execution",
        )

        response = self.client.post(
            "/api/admin/workflows/workflow-blocked-fresh-1/attempts",
            json={"mode": "fresh"},
            auth=("admin", "secret"),
        )
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertNotEqual(body["workflow_id"], "workflow-blocked-fresh-1")
        self.assertEqual(body["attempt_number"], 1)
        self.assertEqual(body["entry_mode"], "fresh")
        self.assertEqual(body["entry_stage"], "orchestrated")
        self.assertIsNone(body["entry_checkpoint_id"])

        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            workflow = session.get(WorkflowExecution, body["workflow_id"])
            self.assertIsNotNone(workflow)
            assert workflow is not None
            self.assertEqual(workflow.source_workflow_id, "workflow-blocked-fresh-1")
            self.assertEqual(workflow.source_run_id, "run-blocked-fresh-1")
            self.assertEqual(workflow.status, "queued")

    def test_fresh_workflow_attempt_rejects_checkpoint_kind(self) -> None:
        payload = self._tenant_payload()
        self._insert_jira_connection(connection_id="conn-1")
        create_tenant = self.client.post("/api/admin/tenants", json=payload, auth=("admin", "secret"))
        self.assertEqual(create_tenant.status_code, 201)

        self._seed_workflow_attempt(
            workflow_id="workflow-terminal-fresh-invalid",
            run_id="run-terminal-fresh-invalid",
            issue_key="TP-1003",
            issue_summary="Terminal workflow invalid fresh",
            workflow_status="failed",
            run_status="failed",
            checkpoint_id="checkpoint-terminal-fresh-invalid",
            checkpoint_kind="pm",
        )

        response = self.client.post(
            "/api/admin/workflows/workflow-terminal-fresh-invalid/attempts",
            json={"mode": "fresh", "checkpoint_kind": "pm"},
            auth=("admin", "secret"),
        )
        self.assertEqual(response.status_code, 422, response.text)

    def test_cancel_active_run_from_admin(self) -> None:
        payload = self._tenant_payload()
        self._insert_jira_connection(connection_id="conn-1")
        create_tenant = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_tenant.status_code, 201)
        self._seed_workflow_attempt(
            workflow_id="workflow-active-cancel",
            run_id="run-active-cancel",
            issue_key="TP-998",
            issue_summary="active run",
            workflow_status="running",
            run_status="running",
        )

        response = self.client.post("/api/admin/runs/run-active-cancel/cancel", auth=("admin", "secret"))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["run_id"], "run-active-cancel")
        self.assertEqual(body["status"], "cancelled")

        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            cancelled = session.get(Run, "run-active-cancel")
            self.assertIsNotNone(cancelled)
            assert cancelled is not None
            self.assertEqual(cancelled.status, "cancelled")
            workflow = session.get(WorkflowExecution, "workflow-active-cancel")
            self.assertIsNotNone(workflow)
            assert workflow is not None
            self.assertEqual(workflow.status, "cancelled")

    def test_cancel_terminal_run_from_admin_returns_conflict(self) -> None:
        payload = self._tenant_payload()
        self._insert_jira_connection(connection_id="conn-1")
        create_tenant = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_tenant.status_code, 201)
        self._seed_workflow_attempt(
            workflow_id="workflow-terminal-cancel",
            run_id="run-terminal-cancel",
            issue_key="TP-997",
            issue_summary="terminal run",
            workflow_status="failed",
            run_status="failed",
        )

        response = self.client.post("/api/admin/runs/run-terminal-cancel/cancel", auth=("admin", "secret"))
        self.assertEqual(response.status_code, 409)
        self.assertIn("Cannot cancel run", response.json()["detail"])

    def test_list_run_events_for_run(self) -> None:
        payload = self._tenant_payload()
        self._insert_jira_connection(connection_id="conn-1")
        create_tenant = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_tenant.status_code, 201)
        now = datetime.now(timezone.utc)
        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            add_workflow_attempt(
                session,
                run_id="run-events-1",
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                issue_key="TP-501",
                issue_summary="event run",
                issue_description="desc",
                repo_url="https://github.com/example/repo",
                created_at=now,
                run_status="running",
                workflow_status="running",
                started_at=now,
            )
            record_agent_lifecycle_event(
                session=session,
                event_type="TASK_STARTED",
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                run_id="run-events-1",
                issue_key="TP-501",
                agent_id="worker-1",
                recorded_at=now,
            )
            session.commit()

        response = self.client.get("/api/admin/runs/run-events-1/events", auth=("admin", "secret"))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]["run_id"], "run-events-1")
        self.assertEqual(body[0]["event_type"], "TASK_STARTED")

    def test_list_run_logs_for_run(self) -> None:
        payload = self._tenant_payload()
        self._insert_jira_connection(connection_id="conn-1")
        create_tenant = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_tenant.status_code, 201)
        now = datetime.now(timezone.utc)
        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            add_workflow_attempt(
                session,
                run_id="run-log-1",
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                issue_key="TP-503",
                issue_summary="event log run",
                issue_description="desc",
                repo_url="https://github.com/example/repo",
                created_at=now,
                run_status="running",
                workflow_status="running",
                started_at=now,
            )
            session.flush()
            record_run_log_event(
                session=session,
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                run_id="run-log-1",
                issue_key="TP-503",
                agent_id="worker-logs",
                invocation_id="inv-run-log-1",
                channel="worker",
                command="workflow.dev",
                working_dir="/tmp/repo",
                stage="dev",
                attempt=1,
                stream="stdout",
                message="hello from codex",
                recorded_at=now,
            )
            session.commit()

        response = self.client.get("/api/admin/runs/run-log-1/logs", auth=("admin", "secret"))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]["run_id"], "run-log-1")
        self.assertEqual(body[0]["message"], "hello from codex")

    def test_list_run_logs_supports_cursor_pagination(self) -> None:
        payload = self._tenant_payload()
        self._insert_jira_connection(connection_id="conn-1")
        create_tenant = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_tenant.status_code, 201)
        now = datetime.now(timezone.utc)
        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            add_workflow_attempt(
                session,
                run_id="run-log-page",
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                issue_key="TP-504",
                issue_summary="event log page run",
                issue_description="desc",
                repo_url="https://github.com/example/repo",
                created_at=now,
                run_status="running",
                workflow_status="running",
                started_at=now,
            )
            session.flush()
            record_run_log_event(
                session=session,
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                run_id="run-log-page",
                issue_key="TP-504",
                agent_id="worker-logs",
                invocation_id="inv-run-log-page",
                channel="worker",
                command="workflow.dev",
                working_dir="/tmp/repo",
                stage="dev",
                attempt=1,
                stream="stdout",
                message="line-1",
                recorded_at=now - timedelta(seconds=2),
            )
            record_run_log_event(
                session=session,
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                run_id="run-log-page",
                issue_key="TP-504",
                agent_id="worker-logs",
                invocation_id="inv-run-log-page",
                channel="worker",
                command="workflow.dev",
                working_dir="/tmp/repo",
                stage="dev",
                attempt=1,
                stream="stdout",
                message="line-2",
                recorded_at=now - timedelta(seconds=1),
            )
            record_run_log_event(
                session=session,
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                run_id="run-log-page",
                issue_key="TP-504",
                agent_id="worker-logs",
                invocation_id="inv-run-log-page",
                channel="worker",
                command="workflow.dev",
                working_dir="/tmp/repo",
                stage="dev",
                attempt=1,
                stream="stdout",
                message="line-3",
                recorded_at=now,
            )
            session.commit()

        first_page = self.client.get(
            "/api/admin/runs/run-log-page/logs?limit=2",
            auth=("admin", "secret"),
        )
        self.assertEqual(first_page.status_code, 200)
        first_body = first_page.json()
        self.assertEqual([entry["message"] for entry in first_body], ["line-3", "line-2"])

        second_page = self.client.get(
            f"/api/admin/runs/run-log-page/logs?limit=2&before_recorded_at={quote_plus(first_body[-1]['recorded_at'])}",
            auth=("admin", "secret"),
        )
        self.assertEqual(second_page.status_code, 200)
        second_body = second_page.json()
        self.assertEqual([entry["message"] for entry in second_body], ["line-1"])

    def test_stream_run_events_returns_ndjson_snapshot(self) -> None:
        payload = self._tenant_payload()
        self._insert_jira_connection(connection_id="conn-1")
        create_tenant = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_tenant.status_code, 201)
        now = datetime.now(timezone.utc)
        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            add_workflow_attempt(
                session,
                run_id="run-events-stream",
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                issue_key="TP-502",
                issue_summary="event stream run",
                issue_description="desc",
                repo_url="https://github.com/example/repo",
                created_at=now,
                run_status="running",
                workflow_status="running",
                started_at=now,
            )
            record_agent_lifecycle_event(
                session=session,
                event_type="TASK_STARTED",
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                run_id="run-events-stream",
                issue_key="TP-502",
                agent_id="worker-stream",
                recorded_at=now,
            )
            record_run_log_event(
                session=session,
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                run_id="run-events-stream",
                issue_key="TP-502",
                agent_id="worker-stream",
                invocation_id="inv-run-events-stream",
                channel="worker",
                command="workflow.dev",
                working_dir="/tmp/repo",
                stage="dev",
                attempt=1,
                stream="stdout",
                message="live line",
                recorded_at=now,
            )
            session.commit()

        response = self.client.get("/api/admin/runs/run-events-stream/events/stream", auth=("admin", "secret"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("content-type"), "application/x-ndjson")
        self.assertIn("\"run_id\":\"run-events-stream\"", response.text)
        self.assertIn("\"event_type\":\"TASK_STARTED\"", response.text)
        self.assertIn("\"event_kind\":\"codex_log\"", response.text)
        self.assertIn("\"message\":\"live line\"", response.text)

    def test_stream_run_events_missing_run_returns_not_found(self) -> None:
        response = self.client.get("/api/admin/runs/run-missing/events/stream", auth=("admin", "secret"))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "Run not found")

    def test_list_agent_activity_returns_dark_and_active_agents(self) -> None:
        payload = self._tenant_payload()
        create_response = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_response.status_code, 201)

        now = datetime.now(timezone.utc)
        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            record_agent_lifecycle_event(
                session=session,
                event_type="TASK_STARTED",
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                run_id="run-active",
                issue_key="TP-1",
                agent_id="worker-active",
                recorded_at=now,
            )
            record_agent_lifecycle_event(
                session=session,
                event_type="TASK_FAILED",
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                run_id="run-stale",
                issue_key="TP-2",
                agent_id="worker-stale",
                recorded_at=now - timedelta(minutes=20),
            )
            session.commit()

        response = self.client.get(
            "/api/admin/agents/activity?tenant_id=tenant-a&heartbeat_timeout_seconds=300",
            auth=("admin", "secret"),
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body), 2)
        by_agent = {entry["agent_id"]: entry for entry in body}
        self.assertIn("worker-active", by_agent)
        self.assertIn("worker-stale", by_agent)
        self.assertFalse(by_agent["worker-active"]["is_dark"])
        self.assertTrue(by_agent["worker-stale"]["is_dark"])
        self.assertGreaterEqual(len(by_agent["worker-active"]["events"]), 1)

    def test_project_execution_metrics_includes_queue_duration_and_sla_signals(self) -> None:
        payload = self._tenant_payload()
        create_response = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_response.status_code, 201)

        project_id = "tenant-a-default"
        now = datetime.now(timezone.utc)
        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            add_workflow_attempt(
                session,
                run_id="metrics-run-queued-stale",
                tenant_id="tenant-a",
                project_id=project_id,
                issue_key="MBAPP-1",
                issue_summary="stale queued",
                issue_description="desc",
                repo_url="https://github.com/example/mobile-app",
                created_at=now - timedelta(hours=3),
            )
            add_workflow_attempt(
                session,
                run_id="metrics-run-queued-fresh",
                tenant_id="tenant-a",
                project_id=project_id,
                issue_key="MBAPP-2",
                issue_summary="fresh queued",
                issue_description="desc",
                repo_url="https://github.com/example/mobile-app",
                created_at=now - timedelta(minutes=30),
            )
            add_workflow_attempt(
                session,
                run_id="metrics-run-succeeded",
                tenant_id="tenant-a",
                project_id=project_id,
                issue_key="MBAPP-3",
                issue_summary="succeeded",
                issue_description="desc",
                repo_url="https://github.com/example/mobile-app",
                created_at=now - timedelta(minutes=20),
                run_status="succeeded",
                workflow_status="succeeded",
                started_at=now - timedelta(minutes=18),
                finished_at=now - timedelta(minutes=10),
            )
            add_workflow_attempt(
                session,
                run_id="metrics-run-failed",
                tenant_id="tenant-a",
                project_id=project_id,
                issue_key="MBAPP-4",
                issue_summary="failed",
                issue_description="desc",
                repo_url="https://github.com/example/mobile-app",
                created_at=now - timedelta(minutes=8),
                run_status="failed",
                workflow_status="failed",
                last_error="boom",
                started_at=now - timedelta(minutes=7),
                finished_at=now - timedelta(minutes=1),
            )
            session.commit()

        response = self.client.get(
            f"/api/admin/tenants/tenant-a/projects/{project_id}/metrics?sla_seconds=300&stale_queue_seconds=3600",
            auth=("admin", "secret"),
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["tenant_id"], "tenant-a")
        self.assertEqual(body["project_id"], project_id)
        self.assertEqual(body["tasks_started"], 2)
        self.assertEqual(body["tasks_completed"], 2)
        self.assertEqual(body["tasks_failed"], 1)
        self.assertEqual(body["tasks_blocked"], 0)
        self.assertEqual(body["queue_length"], 2)
        self.assertEqual(body["stale_queued_tasks"], 1)
        self.assertEqual(body["sla_breaches"], 2)
        self.assertGreater(body["average_duration_seconds"], 0.0)
        self.assertEqual(body["p95_duration_seconds"], 480.0)
        self.assertGreater(body["average_time_in_queue_seconds"], 0.0)
        self.assertEqual(body["success_rate_ratio"], 0.5)

    def test_project_execution_metrics_returns_404_for_mismatched_scope(self) -> None:
        payload = self._tenant_payload()
        create_tenant = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_tenant.status_code, 201)

        response = self.client.get(
            "/api/admin/tenants/tenant-b/projects/tenant-a-default/metrics",
            auth=("admin", "secret"),
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "Project not found")

    def test_github_secret_resolution_prefers_platform_scope_for_unscoped_refs(self) -> None:
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

        def _fake_factory(
            config: dict,
            *,
            tenant_secret_lookup,
            platform_secret_lookup,
            **_: object,
        ):  # noqa: ANN001
            captured["app_id"] = platform_secret_lookup(config["app_id_ref"])
            captured["private_key"] = platform_secret_lookup(config["private_key_ref"])
            return object()

        with patch("orchestrator.api.admin.integration_dependencies.github_client_from_tenant_config", side_effect=_fake_factory):
            response = self.client.post(
                "/api/admin/tenants/tenant-a/test-github",
                auth=("admin", "secret"),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["app_id"], "platform-app-id")
        self.assertEqual(captured["private_key"], "platform-private-key")

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

    def test_delete_tenant_removes_orphan_users_and_tenant_scoped_secrets(self) -> None:
        payload = self._tenant_payload()
        create_response = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_response.status_code, 201)

        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            now = datetime.now(timezone.utc)
            user = TenantUser(
                user_id="user-1",
                email="owner@example.com",
                full_name="Owner Example",
                is_active=True,
                created_at=now,
                updated_at=now,
            )
            session.add(user)
            session.add(
                TenantUserCredential(
                    user_id="user-1",
                    password_hash="hash",
                    password_updated_at=now,
                    must_change_password=False,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                TenantMembership(
                    membership_id="membership-1",
                    tenant_id="tenant-a",
                    user_id="user-1",
                    role="tenant_admin",
                    mode_override=None,
                    onboarding_kind="tenant_admin_setup",
                    first_signed_in_at=None,
                    onboarding_completed_at=now,
                    onboarding_version=None,
                    discord_state={},
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                ManagedSecret(
                    secret_ref="tenant/tenant-a/GITHUB_TOKEN",
                    value_encrypted=encrypt_value(
                        plaintext="secret",
                        encryption_key=os.environ["ORCHESTRATOR_SECRETS_ENCRYPTION_KEY"],
                    ),
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                ManagedSecret(
                    secret_ref="project/tenant-a/project-1/API_KEY",
                    value_encrypted=encrypt_value(
                        plaintext="secret",
                        encryption_key=os.environ["ORCHESTRATOR_SECRETS_ENCRYPTION_KEY"],
                    ),
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

        delete_response = self.client.delete("/api/admin/tenants/tenant-a", auth=("admin", "secret"))
        self.assertEqual(delete_response.status_code, 204)

        with session_factory() as session:
            self.assertIsNone(session.get(Tenant, "tenant-a"))
            self.assertIsNone(session.get(TenantUser, "user-1"))
            self.assertIsNone(session.get(TenantUserCredential, "user-1"))
            self.assertIsNone(session.get(ManagedSecret, "tenant/tenant-a/GITHUB_TOKEN"))
            self.assertIsNone(session.get(ManagedSecret, "project/tenant-a/project-1/API_KEY"))

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
        self.assertIsNotNone(archive_response.json()["archived_at"])
        self.assertIsNotNone(archive_response.json()["purge_after_at"])
        archived_at = datetime.fromisoformat(archive_response.json()["archived_at"].replace("Z", "+00:00"))
        purge_after_at = datetime.fromisoformat(archive_response.json()["purge_after_at"].replace("Z", "+00:00"))
        self.assertEqual((purge_after_at - archived_at).days, 60)

        unarchive_response = self.client.post(
            "/api/admin/tenants/tenant-a/unarchive",
            auth=("admin", "secret"),
        )
        self.assertEqual(unarchive_response.status_code, 200)
        self.assertTrue(unarchive_response.json()["is_enabled"])
        self.assertIsNone(unarchive_response.json()["archived_at"])
        self.assertIsNone(unarchive_response.json()["purge_after_at"])

    def test_purge_expired_archived_tenant_deletes_owned_data(self) -> None:
        payload = self._tenant_payload()
        create_response = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_response.status_code, 201)

        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            now = datetime.now(timezone.utc)
            user = TenantUser(
                user_id="user-1",
                email="owner@example.com",
                full_name="Owner Example",
                is_active=True,
                created_at=now,
                updated_at=now,
            )
            session.add(user)
            session.add(
                TenantUserCredential(
                    user_id="user-1",
                    password_hash="hash",
                    password_updated_at=now,
                    must_change_password=False,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                TenantUserDiscordIdentity(
                    user_id="user-1",
                    discord_user_id="discord-user-1",
                    discord_username="owner",
                    discord_global_name="Owner Example",
                    discord_avatar_hash=None,
                    linked_at=now,
                    updated_at=now,
                )
            )
            session.add(
                TenantMembership(
                    membership_id="membership-1",
                    tenant_id="tenant-a",
                    user_id="user-1",
                    role="tenant_admin",
                    mode_override=None,
                    onboarding_kind="tenant_admin_setup",
                    first_signed_in_at=None,
                    onboarding_completed_at=now,
                    onboarding_version=None,
                    discord_state={},
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                TenantTeam(
                    team_id="team-1",
                    tenant_id="tenant-a",
                    name="Operations",
                    description="Ops access",
                    permission_keys=["workspace.manage"],
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                TenantTeamMembership(
                    team_membership_id="team-membership-1",
                    team_id="team-1",
                    membership_id="membership-1",
                    created_at=now,
                )
            )
            session.add(
                TenantInvite(
                    invite_id="invite-1",
                    tenant_id="tenant-a",
                    email="invitee@example.com",
                    full_name="Invitee Example",
                    role="business_member",
                    team_ids=["team-1"],
                    mode_override=None,
                    status="pending",
                    invite_token_hash="invite-hash-1",
                    invited_by_user_id="user-1",
                    accepted_by_user_id=None,
                    expires_at=now + timedelta(days=7),
                    accepted_at=None,
                    revoked_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                ManagedSecret(
                    secret_ref="tenant/tenant-a/GITHUB_TOKEN",
                    value_encrypted=encrypt_value(
                        plaintext="secret",
                        encryption_key=os.environ["ORCHESTRATOR_SECRETS_ENCRYPTION_KEY"],
                    ),
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                ManagedSecret(
                    secret_ref="project/tenant-a/project-1/API_KEY",
                    value_encrypted=encrypt_value(
                        plaintext="secret",
                        encryption_key=os.environ["ORCHESTRATOR_SECRETS_ENCRYPTION_KEY"],
                    ),
                    created_at=now,
                    updated_at=now,
                )
            )
            add_workflow_attempt(
                session,
                run_id="run-1",
                tenant_id="tenant-a",
                project_id=None,
                issue_key="TP-1",
                issue_summary="Test run",
                issue_description=None,
                repo_url=None,
                created_at=now,
            )
            session.commit()

        archive_response = self.client.post(
            "/api/admin/tenants/tenant-a/archive",
            auth=("admin", "secret"),
        )
        self.assertEqual(archive_response.status_code, 200)
        purge_after_at = datetime.fromisoformat(archive_response.json()["purge_after_at"].replace("Z", "+00:00"))

        with session_factory() as session:
            purged = purge_expired_archived_tenants(
                session=session,
                now=purge_after_at + timedelta(seconds=1),
            )
        self.assertEqual(purged, 1)

        with session_factory() as session:
            self.assertIsNone(session.get(Tenant, "tenant-a"))
            self.assertIsNone(session.get(TenantRunClaim, "tenant-a"))
            self.assertIsNone(session.get(TenantUser, "user-1"))
            self.assertIsNone(session.get(TenantUserCredential, "user-1"))
            self.assertIsNone(session.get(TenantUserDiscordIdentity, "user-1"))
            self.assertIsNone(session.get(TenantTeam, "team-1"))
            self.assertIsNone(session.get(TenantInvite, "invite-1"))
            self.assertIsNone(session.get(ManagedSecret, "tenant/tenant-a/GITHUB_TOKEN"))
            self.assertIsNone(session.get(ManagedSecret, "project/tenant-a/project-1/API_KEY"))
            self.assertIsNone(session.get(Run, "run-1"))

    def test_purge_expired_archived_tenant_respects_retention_window(self) -> None:
        payload = self._tenant_payload()
        create_response = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_response.status_code, 201)

        archive_response = self.client.post(
            "/api/admin/tenants/tenant-a/archive",
            auth=("admin", "secret"),
        )
        self.assertEqual(archive_response.status_code, 200)
        purge_after_at = datetime.fromisoformat(archive_response.json()["purge_after_at"].replace("Z", "+00:00"))
        session_factory = create_session_factory(self.database_url)

        with session_factory() as session:
            purged = purge_expired_archived_tenants(
                session=session,
                now=purge_after_at - timedelta(seconds=1),
            )
        self.assertEqual(purged, 0)

        still_present = self.client.get("/api/admin/tenants/tenant-a", auth=("admin", "secret"))
        self.assertEqual(still_present.status_code, 200)

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

        response = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["repos"]["allowlist"], [])

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
        self.assertIn("/tenants/tenant-a/settings/github?github_install=success", callback_response.headers.get("location", ""))

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

    def test_start_install_fails_when_platform_slug_missing(self) -> None:
        payload = self._tenant_payload()
        payload["github"]["installation_id"] = None
        create_response = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_response.status_code, 201)

        delete_response = self.client.delete(
            "/api/admin/secrets/platform%2FGITHUB_APP_SLUG",
            auth=("admin", "secret"),
        )
        self.assertEqual(delete_response.status_code, 204)

        start_response = self.client.post(
            "/api/admin/tenants/tenant-a/github/install/start",
            auth=("admin", "secret"),
        )
        self.assertEqual(start_response.status_code, 400)
        self.assertEqual(start_response.json()["detail"], "GitHub app slug is not configured")

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

        with patch("orchestrator.api.admin.integration_dependencies.github_client_from_tenant_config", return_value=_FakeClient()):
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
            "orchestrator.api.admin.integration_dependencies.github_client_from_tenant_config",
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

        with patch("orchestrator.api.admin.integration_dependencies.jira_oauth_client", return_value=_FakeClient()):
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

        with (
            patch("orchestrator.api.admin.integration_dependencies.jira_oauth_client", return_value=_FakeClient()),
            patch(
                "orchestrator.api.admin.integration_dependencies.provision_jira_webhook",
                return_value=SimpleNamespace(ok=True),
            ) as provision_mock,
        ):
            callback_response = self.client.get(
                "/api/admin/jira/connect/callback",
                params={"code": "abc123", "state": state_token},
                follow_redirects=False,
            )

        self.assertEqual(callback_response.status_code, 302)
        self.assertIn(
            "/tenants/tenant-a/settings/jira?jira_oauth=success&jira_connection_id=",
            callback_response.headers.get("location", ""),
        )
        self.assertIn("jira_webhook=ok", callback_response.headers.get("location", ""))
        provision_mock.assert_called_once()
        self.assertTrue(provision_mock.call_args.kwargs["replace_existing"])

        tenant_response = self.client.get("/api/admin/tenants/tenant-a", auth=("admin", "secret"))
        self.assertEqual(tenant_response.status_code, 200)
        self.assertTrue(tenant_response.json()["jira"]["connection_id"])
        self.assertIsInstance(tenant_response.json()["jira"]["managed_webhook_ids"], list)

    def test_jira_connect_edit_callback_reports_webhook_provision_failure_in_redirect(self) -> None:
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

        with (
            patch("orchestrator.api.admin.integration_dependencies.jira_oauth_client", return_value=_FakeClient()),
            patch(
                "orchestrator.api.admin.integration_dependencies.provision_jira_webhook",
                side_effect=RuntimeError("provision-failed"),
            ) as provision_mock,
        ):
            callback_response = self.client.get(
                "/api/admin/jira/connect/callback",
                params={"code": "abc123", "state": state_token},
                follow_redirects=False,
            )

        self.assertEqual(callback_response.status_code, 302)
        self.assertIn("jira_webhook=failed", callback_response.headers.get("location", ""))
        provision_mock.assert_called_once()
        self.assertTrue(provision_mock.call_args.kwargs["replace_existing"])

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
            patch("orchestrator.api.admin.integration_dependencies.refresh_jira_connection_tokens", return_value="access-token"),
            patch("orchestrator.api.admin.integration_dependencies.jira_oauth_client", return_value=_FakeClient()),
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
            patch("orchestrator.api.admin.integration_dependencies.refresh_jira_connection_tokens", return_value="access-token"),
            patch("orchestrator.api.admin.integration_dependencies.jira_oauth_client", return_value=_FakeClient()),
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
            patch("orchestrator.api.admin.integration_dependencies.refresh_jira_connection_tokens", return_value="access-token"),
            patch("orchestrator.api.admin.integration_dependencies.jira_oauth_client", return_value=fake_client),
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
            patch("orchestrator.api.admin.integration_dependencies.refresh_jira_connection_tokens", return_value="access-token"),
            patch("orchestrator.api.admin.integration_dependencies.jira_oauth_client", return_value=fake_client),
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
            patch("orchestrator.api.admin.integration_dependencies.refresh_jira_connection_tokens", return_value="access-token"),
            patch("orchestrator.api.admin.integration_dependencies.jira_oauth_client", return_value=fake_client),
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
            "/api/admin/secrets/platform%2FDISCORD_BOT_TOKEN",
            json={"value": "test-discord-bot-token"},
            auth=("admin", "secret"),
        )
        self.assertEqual(seed_token_secret.status_code, 200)

        with patch("orchestrator.tools.discord_api.DiscordApiClient.send_direct_message", return_value={"id": "msg-1"}):
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
            patch("orchestrator.api.admin.integration_dependencies.refresh_jira_connection_tokens", return_value="access-token"),
            patch("orchestrator.api.admin.integration_dependencies.jira_oauth_client", return_value=fake_client),
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
            patch("orchestrator.api.admin.integration_dependencies.refresh_jira_connection_tokens", return_value="access-token"),
            patch("orchestrator.api.admin.integration_dependencies.jira_oauth_client", return_value=_FakeClient()),
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

        with patch("orchestrator.api.admin.integration_dependencies.provision_jira_webhook", side_effect=RuntimeError("boom")):
            with TestClient(create_app(), raise_server_exceptions=False) as non_raising_client:
                response = non_raising_client.post(
                    "/api/admin/tenants/tenant-a/jira/webhooks/reset",
                    auth=("admin", "secret"),
                )

        self.assertEqual(response.status_code, 500)
        detail = response.json().get("detail", "")
        self.assertTrue(detail.startswith("Internal server error. Ref: "))

    def test_update_project_knowledge_asset_status_approves_pending_review_asset(self) -> None:
        payload = self._tenant_payload()
        create_response = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_response.status_code, 201)

        now = datetime.now(timezone.utc)
        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            asset = KnowledgeAsset(
                asset_id="kb-pending-1",
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                source_type="decision_answer",
                title="Decision answer candidate",
                mime_type="text/plain",
                source_ref="TP-101:dg_abc123",
                source_timestamp=now,
                checksum="checksum-1",
                text_content="Accepted answer text",
                binary_content=None,
                chunk_count=0,
                status="pending_review",
                metadata_json={"question_id": "dg_abc123"},
                created_at=now,
                updated_at=now,
            )
            session.add(asset)
            session.add(
                KnowledgeFact(
                    fact_id="kb-pending-fact-1",
                    asset_id="kb-pending-1",
                    chunk_id=None,
                    tenant_id="tenant-a",
                    project_id="tenant-a-default",
                    fact_type="decision_slot",
                    fact_key="decision_owner",
                    fact_value="Platform owner",
                    approval_state="pending_review",
                    slot_name="decision_owner",
                    slot_value="Platform owner",
                    confidence=0.9,
                    is_inferred=False,
                    metadata_json={},
                    source_timestamp=now,
                    superseded_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

        response = self.client.patch(
            "/api/admin/tenants/tenant-a/projects/tenant-a-default/knowledge/assets/kb-pending-1/status",
            json={"status": "ready"},
            auth=("admin", "secret"),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ready")

        with session_factory() as session:
            asset = session.get(KnowledgeAsset, "kb-pending-1")
            self.assertIsNotNone(asset)
            self.assertEqual(asset.status, "ready")
            fact = session.get(KnowledgeFact, "kb-pending-fact-1")
            self.assertIsNotNone(fact)
            self.assertEqual(fact.approval_state, "approved")

    def test_update_project_knowledge_asset_status_rejects_invalid_transition(self) -> None:
        payload = self._tenant_payload()
        create_response = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_response.status_code, 201)

        now = datetime.now(timezone.utc)
        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            asset = KnowledgeAsset(
                asset_id="kb-ready-1",
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                source_type="decision_answer",
                title="Published decision answer",
                mime_type="text/plain",
                source_ref="TP-101:dg_xyz789",
                source_timestamp=now,
                checksum="checksum-2",
                text_content="Published answer text",
                binary_content=None,
                chunk_count=0,
                status="ready",
                metadata_json={"question_id": "dg_xyz789"},
                created_at=now,
                updated_at=now,
            )
            session.add(asset)
            session.commit()

        response = self.client.patch(
            "/api/admin/tenants/tenant-a/projects/tenant-a-default/knowledge/assets/kb-ready-1/status",
            json={"status": "rejected"},
            auth=("admin", "secret"),
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("cannot transition", response.json()["detail"])

    def test_update_project_knowledge_asset_status_moves_rejected_asset_back_to_review(self) -> None:
        payload = self._tenant_payload()
        create_response = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_response.status_code, 201)

        now = datetime.now(timezone.utc)
        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            asset = KnowledgeAsset(
                asset_id="kb-rejected-1",
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                source_type="decision_answer",
                title="Rejected decision answer",
                mime_type="text/plain",
                source_ref="TP-101:dg_rejected",
                source_timestamp=now,
                checksum="checksum-3",
                text_content="Rejected answer text",
                binary_content=None,
                chunk_count=0,
                status="rejected",
                metadata_json={"question_id": "dg_rejected"},
                created_at=now,
                updated_at=now,
            )
            session.add(asset)
            session.commit()

        response = self.client.patch(
            "/api/admin/tenants/tenant-a/projects/tenant-a-default/knowledge/assets/kb-rejected-1/status",
            json={"status": "pending_review"},
            auth=("admin", "secret"),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "pending_review")

    def test_list_project_knowledge_assets_supports_paging_and_filters(self) -> None:
        payload = self._tenant_payload()
        create_response = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_response.status_code, 201)

        now = datetime.now(timezone.utc)
        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            session.add_all(
                [
                    KnowledgeAsset(
                        asset_id="kb-1",
                        tenant_id="tenant-a",
                        project_id="tenant-a-default",
                        source_type="jira_issue",
                        title="MAB-100 auth flow",
                        mime_type="text/plain",
                        source_ref="jira:issue:MAB-100",
                        source_timestamp=now,
                        checksum="checksum-a",
                        text_content="Auth flow details",
                        binary_content=None,
                        chunk_count=2,
                        status="ready",
                        metadata_json={},
                        created_at=now,
                        updated_at=now,
                    ),
                    KnowledgeAsset(
                        asset_id="kb-2",
                        tenant_id="tenant-a",
                        project_id="tenant-a-default",
                        source_type="file_upload",
                        title="Architecture notes",
                        mime_type="text/markdown",
                        source_ref="notes.md",
                        source_timestamp=now,
                        checksum="checksum-b",
                        text_content="Platform notes",
                        binary_content=None,
                        chunk_count=1,
                        status="pending_review",
                        metadata_json={},
                        created_at=now,
                        updated_at=now + timedelta(seconds=1),
                    ),
                    KnowledgeAsset(
                        asset_id="kb-3",
                        tenant_id="tenant-a",
                        project_id="tenant-a-default",
                        source_type="jira_comment",
                        title="MAB-133 decision",
                        mime_type="text/plain",
                        source_ref="jira:comment:MAB-133:42",
                        source_timestamp=now,
                        checksum="checksum-c",
                        text_content="Decision comment",
                        binary_content=None,
                        chunk_count=1,
                        status="ready",
                        metadata_json={},
                        created_at=now,
                        updated_at=now + timedelta(seconds=2),
                    ),
                ]
            )
            session.commit()

        response = self.client.get(
            "/api/admin/tenants/tenant-a/projects/tenant-a-default/knowledge/assets?limit=1&offset=0&status=ready&q=MAB",
            auth=("admin", "secret"),
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total"], 2)
        self.assertEqual(body["limit"], 1)
        self.assertEqual(body["offset"], 0)
        self.assertEqual(len(body["items"]), 1)
        self.assertEqual(body["items"][0]["asset_id"], "kb-3")

    def test_get_project_knowledge_asset_detail_and_chunk_page(self) -> None:
        payload = self._tenant_payload()
        create_response = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_response.status_code, 201)

        now = datetime.now(timezone.utc)
        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            asset = KnowledgeAsset(
                asset_id="kb-detail-1",
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                source_type="file_upload",
                title="Design notes",
                mime_type="text/plain",
                source_ref="design.txt",
                source_timestamp=now,
                checksum="checksum-detail",
                text_content="Detailed knowledge content",
                binary_content=None,
                chunk_count=3,
                status="ready",
                metadata_json={"origin": "upload"},
                created_at=now,
                updated_at=now,
            )
            session.add(asset)
            session.add_all(
                [
                    KnowledgeChunk(
                        chunk_id="chunk-1",
                        asset_id="kb-detail-1",
                        tenant_id="tenant-a",
                        project_id="tenant-a-default",
                        chunk_index=0,
                        content="Chunk zero",
                        token_count=2,
                        embedding=None,
                        source_timestamp=now,
                        created_at=now,
                        updated_at=now,
                    ),
                    KnowledgeChunk(
                        chunk_id="chunk-2",
                        asset_id="kb-detail-1",
                        tenant_id="tenant-a",
                        project_id="tenant-a-default",
                        chunk_index=1,
                        content="Chunk one",
                        token_count=2,
                        embedding=None,
                        source_timestamp=now,
                        created_at=now,
                        updated_at=now,
                    ),
                    KnowledgeFact(
                        fact_id="fact-1",
                        asset_id="kb-detail-1",
                        chunk_id=None,
                        tenant_id="tenant-a",
                        project_id="tenant-a-default",
                        fact_type="configuration",
                        fact_key="production_bundle_id",
                        fact_value="com.example.girlpower",
                        approval_state="approved",
                        slot_name="production_bundle_id",
                        slot_value="com.example.girlpower",
                        confidence=0.9,
                        is_inferred=False,
                        metadata_json={"label": "Production Bundle ID"},
                        source_timestamp=now,
                        superseded_at=None,
                        created_at=now,
                        updated_at=now,
                    ),
                ]
            )
            session.commit()

        detail_response = self.client.get(
            "/api/admin/tenants/tenant-a/projects/tenant-a-default/knowledge/assets/kb-detail-1",
            auth=("admin", "secret"),
        )
        self.assertEqual(detail_response.status_code, 200)
        self.assertEqual(detail_response.json()["text_content"], "Detailed knowledge content")
        self.assertEqual(detail_response.json()["metadata_json"]["origin"], "upload")
        self.assertEqual(detail_response.json()["facts"][0]["fact_key"], "production_bundle_id")

        chunks_response = self.client.get(
            "/api/admin/tenants/tenant-a/projects/tenant-a-default/knowledge/assets/kb-detail-1/chunks?limit=1&offset=1",
            auth=("admin", "secret"),
        )
        self.assertEqual(chunks_response.status_code, 200)
        chunks_body = chunks_response.json()
        self.assertEqual(chunks_body["total"], 2)
        self.assertEqual(len(chunks_body["items"]), 1)
        self.assertEqual(chunks_body["items"][0]["chunk_id"], "chunk-2")

    def test_get_project_knowledge_stats_returns_aggregates(self) -> None:
        payload = self._tenant_payload()
        create_response = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_response.status_code, 201)

        now = datetime.now(timezone.utc)
        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            session.add_all(
                [
                    KnowledgeAsset(
                        asset_id="stats-1",
                        tenant_id="tenant-a",
                        project_id="tenant-a-default",
                        source_type="jira_issue",
                        title="Ready issue",
                        mime_type="text/plain",
                        source_ref="jira:issue:1",
                        source_timestamp=now,
                        checksum="stats-1",
                        text_content="Ready",
                        binary_content=None,
                        chunk_count=4,
                        status="ready",
                        metadata_json={},
                        created_at=now,
                        updated_at=now,
                    ),
                    KnowledgeAsset(
                        asset_id="stats-2",
                        tenant_id="tenant-a",
                        project_id="tenant-a-default",
                        source_type="file_upload",
                        title="Pending asset",
                        mime_type="text/plain",
                        source_ref="pending.txt",
                        source_timestamp=now,
                        checksum="stats-2",
                        text_content="Pending",
                        binary_content=None,
                        chunk_count=2,
                        status="pending_review",
                        metadata_json={},
                        created_at=now,
                        updated_at=now + timedelta(seconds=1),
                    ),
                    KnowledgeFact(
                        fact_id="stats-fact-1",
                        asset_id="stats-1",
                        chunk_id=None,
                        tenant_id="tenant-a",
                        project_id="tenant-a-default",
                        fact_type="decision_slot",
                        fact_key="decision_owner",
                        fact_value="Platform owner",
                        approval_state="approved",
                        slot_name="decision_owner",
                        slot_value="Platform owner",
                        confidence=0.95,
                        is_inferred=False,
                        metadata_json={},
                        source_timestamp=now,
                        superseded_at=None,
                        created_at=now,
                        updated_at=now,
                    ),
                    KnowledgeFact(
                        fact_id="stats-fact-2",
                        asset_id="stats-2",
                        chunk_id=None,
                        tenant_id="tenant-a",
                        project_id="tenant-a-default",
                        fact_type="reference_fact",
                        fact_key="rollback_plan",
                        fact_value="Retry on next launch",
                        approval_state="pending_review",
                        slot_name="rollout_constraints",
                        slot_value="Retry on next launch",
                        confidence=0.7,
                        is_inferred=False,
                        metadata_json={},
                        source_timestamp=now,
                        superseded_at=now + timedelta(seconds=2),
                        created_at=now,
                        updated_at=now + timedelta(seconds=2),
                    ),
                ]
            )
            session.commit()

        response = self.client.get(
            "/api/admin/tenants/tenant-a/projects/tenant-a-default/knowledge/stats",
            auth=("admin", "secret"),
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total_assets"], 2)
        self.assertEqual(body["total_chunks"], 6)
        self.assertEqual(body["total_facts"], 2)
        self.assertEqual(body["approved_facts"], 1)
        self.assertEqual(body["pending_review_facts"], 1)
        self.assertEqual(body["superseded_facts"], 1)
        self.assertEqual(body["ready_assets"], 1)
        self.assertEqual(body["pending_review_assets"], 1)
        self.assertEqual(body["rejected_assets"], 0)
        self.assertEqual(body["source_type_counts"]["jira_issue"], 1)

    def test_project_knowledge_sources_support_crud(self) -> None:
        payload = self._tenant_payload()
        create_response = self.client.post("/api/admin/tenants", json=payload, auth=("admin", "secret"))
        self.assertEqual(create_response.status_code, 201)

        create_source = self.client.post(
            "/api/admin/tenants/tenant-a/projects/tenant-a-default/knowledge/sources",
            json={
                "connector_type": "google_drive",
                "display_name": "Architecture Docs",
                "config_json": {"targets": ["https://drive.google.com/file/d/abc123/view"]},
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(create_source.status_code, 201)
        source_body = create_source.json()
        self.assertEqual(source_body["connector_type"], "google_drive")
        self.assertEqual(source_body["status"], "active")
        self.assertFalse(source_body["supports_sync_now"])
        source_id = source_body["source_id"]

        list_response = self.client.get(
            "/api/admin/tenants/tenant-a/projects/tenant-a-default/knowledge/sources",
            auth=("admin", "secret"),
        )
        self.assertEqual(list_response.status_code, 200)
        items = list_response.json()["items"]
        self.assertTrue(any(item["source_id"] == source_id for item in items))

        update_response = self.client.patch(
            f"/api/admin/tenants/tenant-a/projects/tenant-a-default/knowledge/sources/{source_id}",
            json={"status": "disabled"},
            auth=("admin", "secret"),
        )
        self.assertEqual(update_response.status_code, 200)
        self.assertEqual(update_response.json()["status"], "disabled")

        delete_response = self.client.delete(
            f"/api/admin/tenants/tenant-a/projects/tenant-a-default/knowledge/sources/{source_id}",
            auth=("admin", "secret"),
        )
        self.assertEqual(delete_response.status_code, 204)

    def test_sync_project_knowledge_source_rejects_unsupported_connector(self) -> None:
        payload = self._tenant_payload()
        create_response = self.client.post("/api/admin/tenants", json=payload, auth=("admin", "secret"))
        self.assertEqual(create_response.status_code, 201)

        source_response = self.client.post(
            "/api/admin/tenants/tenant-a/projects/tenant-a-default/knowledge/sources",
            json={
                "connector_type": "discord",
                "display_name": "Support Threads",
                "config_json": {"thread_ids": ["1234567890"]},
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(source_response.status_code, 201)
        source_id = source_response.json()["source_id"]

        sync_response = self.client.post(
            f"/api/admin/tenants/tenant-a/projects/tenant-a-default/knowledge/sources/{source_id}/sync",
            auth=("admin", "secret"),
        )
        self.assertEqual(sync_response.status_code, 409)
        self.assertIn("does not provide a live sync adapter", sync_response.json()["detail"])

    def test_sync_project_knowledge_source_runs_jira_connector(self) -> None:
        payload = self._tenant_payload()
        create_response = self.client.post("/api/admin/tenants", json=payload, auth=("admin", "secret"))
        self.assertEqual(create_response.status_code, 201)
        self._insert_jira_connection()

        source_response = self.client.post(
            "/api/admin/tenants/tenant-a/projects/tenant-a-default/knowledge/sources",
            json={"connector_type": "jira", "config_json": {"project_key": "TPA"}},
            auth=("admin", "secret"),
        )
        self.assertEqual(source_response.status_code, 201)
        source_id = source_response.json()["source_id"]

        with (
            patch("orchestrator.core.knowledge_sources.refresh_jira_connection_tokens", return_value="token"),
            patch("orchestrator.core.knowledge_sources.jira_oauth_client", return_value=SimpleNamespace()),
            patch(
                "orchestrator.core.knowledge_sources.sync_project_knowledge_from_jira",
                return_value=SimpleNamespace(
                    ok=True,
                    synced_assets=1,
                    skipped_assets=0,
                    created_assets=1,
                    updated_assets=0,
                    unchanged_assets=0,
                    deleted_assets=0,
                    failed_assets=0,
                    details="synced",
                ),
            ),
        ):
            sync_response = self.client.post(
                f"/api/admin/tenants/tenant-a/projects/tenant-a-default/knowledge/sources/{source_id}/sync",
                auth=("admin", "secret"),
            )
        self.assertEqual(sync_response.status_code, 200)
        self.assertEqual(sync_response.json()["created_assets"], 1)
        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            source = session.get(KnowledgeSource, source_id)
            self.assertIsNotNone(source)
            self.assertIsNotNone(source.last_synced_at)
            self.assertIsNone(source.last_error)

    def test_debug_project_knowledge_search_returns_fact_and_chunk_matches(self) -> None:
        payload = self._tenant_payload()
        create_response = self.client.post(
            "/api/admin/tenants",
            json=payload,
            auth=("admin", "secret"),
        )
        self.assertEqual(create_response.status_code, 201)

        with patch(
            "orchestrator.api.routes.admin_knowledge.search_knowledge_debug",
            return_value=[
                SimpleNamespace(
                    layer="knowledge_fact",
                    score=0.98,
                    asset_id="debug-asset-1",
                    source_type="file_upload",
                    title="Apple auth rollout",
                    source_ref="notes/apple.txt",
                    source_timestamp="2026-01-01T00:00:00+00:00",
                    fact_id="debug-fact-1",
                    chunk_id=None,
                    snippet="com.example.girlpower",
                    metadata={},
                ),
                SimpleNamespace(
                    layer="knowledge_chunk",
                    score=0.95,
                    asset_id="debug-asset-1",
                    source_type="file_upload",
                    title="Apple auth rollout",
                    source_ref="notes/apple.txt",
                    source_timestamp="2026-01-01T00:00:00+00:00",
                    fact_id=None,
                    chunk_id="debug-chunk-1",
                    snippet="Production Bundle ID is com.example.girlpower",
                    metadata={},
                ),
            ],
        ):
            response = self.client.get(
                "/api/admin/tenants/tenant-a/projects/tenant-a-default/knowledge/debug-search?query=production%20bundle%20id",
                auth=("admin", "secret"),
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["query"], "production bundle id")
        self.assertTrue(any(item["layer"] == "knowledge_fact" for item in body["items"]))
        self.assertTrue(any(item["layer"] == "knowledge_chunk" for item in body["items"]))

    def test_admin_knowledge_jira_sync_runtime_status_returns_snapshot(self) -> None:
        with patch(
            "orchestrator.api.routes.admin_observability.get_knowledge_jira_sync_runtime_status",
            return_value=SimpleNamespace(
                state="degraded",
                enabled=True,
                database_backend="postgres",
                started_at=datetime(2026, 3, 13, 11, 7, 17, tzinfo=timezone.utc),
                stopped_at=None,
                last_pass_started_at=datetime(2026, 3, 13, 11, 7, 17, tzinfo=timezone.utc),
                last_pass_finished_at=datetime(2026, 3, 13, 11, 7, 18, tzinfo=timezone.utc),
                last_heartbeat_at=datetime(2026, 3, 13, 11, 7, 18, tzinfo=timezone.utc),
                leader_acquired=True,
                service_instance_id="api-sync-1",
                stale=False,
                projects=(
                    SimpleNamespace(
                        tenant_id="example",
                        project_id="example-default",
                        jira_project_key="GP",
                        state="degraded",
                        failure_category="invalid_refresh_token",
                        last_error="refresh_token is invalid",
                        last_attempted_at=datetime(2026, 3, 13, 11, 7, 17, tzinfo=timezone.utc),
                        last_successful_sync_at=None,
                        next_retry_at=datetime(2026, 3, 13, 17, 7, 17, tzinfo=timezone.utc),
                        consecutive_failures=1,
                    ),
                ),
            ),
        ):
            response = self.client.get(
                "/api/admin/observability/knowledge-jira-sync",
                auth=("admin", "secret"),
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["state"], "degraded")
        self.assertEqual(payload["database_backend"], "postgres")
        self.assertEqual(payload["service_instance_id"], "api-sync-1")
        self.assertFalse(payload["stale"])
        self.assertEqual(len(payload["projects"]), 1)
        self.assertEqual(payload["projects"][0]["failure_category"], "invalid_refresh_token")

    def test_admin_platform_status_reports_hosted_services(self) -> None:
        session_factory = create_session_factory(self.database_url)
        now = datetime.now(timezone.utc)
        with session_factory() as session:
            session.add(
                Tenant(
                    tenant_id="example",
                    name="example",
                    is_enabled=True,
                    jira_config={},
                    github_config={},
                    repos_config={},
                    policy_config={},
                    discord_config={},
                    experience_config={},
                    setup_state={},
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                Project(
                    project_id="example-default",
                    tenant_id="example",
                    name="example Default",
                    github_repository="github.com/example/example",
                    jira_project_key="GP",
                    policy_overrides={},
                    environment={},
                    secret_refs={},
                    discord_config={},
                    is_archived=False,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                WorkerRuntimeState(
                    service_instance_id="worker-1",
                    agent_id="worker-agent-a",
                    worker_mode="runs",
                    capabilities_json=["linux"],
                    state="busy",
                    started_at=now,
                    last_heartbeat_at=now,
                    updated_at=now,
                )
            )
            session.add(
                WorkerRuntimeState(
                    service_instance_id="worker-2",
                    agent_id="worker-agent-b",
                    worker_mode="runs",
                    capabilities_json=["macos"],
                    state="idle",
                    started_at=now,
                    last_heartbeat_at=now,
                    updated_at=now,
                )
            )
            add_workflow_attempt(
                session,
                run_id="run-1",
                tenant_id="example",
                project_id="example-default",
                issue_key="GP-1",
                issue_summary="Issue",
                issue_description=None,
                repo_url=None,
                created_at=now,
                run_status="running",
                workflow_status="running",
                started_at=now,
                last_heartbeat_at=now,
                worker_service_instance_id="worker-1",
            )
            session.add(
                KnowledgeJiraSyncRuntimeState(
                    runtime_name="knowledge-jira-sync",
                    state="running",
                    enabled=True,
                    database_backend="postgresql",
                    started_at=now,
                    stopped_at=None,
                    last_pass_started_at=now,
                    last_pass_finished_at=now,
                    last_heartbeat_at=now,
                    leader_acquired=True,
                    service_instance_id="sync-1",
                    updated_at=now,
                )
            )
            session.add(
                DiscordCommandSyncRuntimeState(
                    runtime_name="discord-command-sync",
                    synced=True,
                    healthy=True,
                    interaction_ingress_ready=True,
                    bot_token_configured=True,
                    guild_id_configured=True,
                    last_attempt_at=now,
                    last_success_at=now,
                    last_failure_reason=None,
                    last_error=None,
                    guild_id="guild-123",
                    application_id="app-123",
                    command_count=7,
                    service_instance_id="discord-1",
                    updated_at=now,
                )
            )
            session.commit()

        with patch.dict(os.environ, {"ORCHESTRATOR_WORKER_CAPABILITIES": "linux,macos"}, clear=False):
            get_settings.cache_clear()
            response = self.client.get("/api/admin/status", auth=("admin", "secret"))
        get_settings.cache_clear()

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(
            [service["service_id"] for service in payload["services"]],
            ["api", "workers", "knowledge_jira_sync", "discord_commands"],
        )
        worker_service = next(service for service in payload["services"] if service["service_id"] == "workers")
        self.assertEqual(worker_service["status"], "healthy")
        self.assertEqual(worker_service["capabilities"], ["Linux", "macOS"])
        instances_by_id = {instance["instance_id"]: instance for instance in worker_service["instances"]}
        self.assertEqual(instances_by_id["worker-1"]["status"], "busy")
        self.assertEqual(instances_by_id["worker-1"]["active_run_count"], 1)
        self.assertEqual(instances_by_id["worker-2"]["status"], "idle")
        self.assertEqual(instances_by_id["worker-2"]["capabilities"], ["macOS"])

    def test_admin_webhook_queue_endpoint_lists_rows_with_summary(self) -> None:
        session_factory = create_session_factory(self.database_url)
        now = datetime.now(timezone.utc)
        with session_factory() as session:
            session.add(
                Tenant(
                    tenant_id="example",
                    name="example",
                    is_enabled=True,
                    jira_config={},
                    github_config={},
                    repos_config={},
                    policy_config={},
                    discord_config={},
                    experience_config={},
                    setup_state={},
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                Project(
                    project_id="example-default",
                    tenant_id="example",
                    name="example Default",
                    github_repository="github.com/example/example",
                    jira_project_key="GP",
                    policy_overrides={},
                    environment={},
                    secret_refs={},
                    discord_config={},
                    is_archived=False,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add_all(
                [
                    WebhookJob(
                        job_id="job-pending-1",
                        transport="github_webhook",
                        tenant_id="example",
                        project_id="example-default",
                        subject_key="github_pr:example:repo:26",
                        dedupe_key="delivery-1",
                        request_id="request-1",
                        event_type="pull_request",
                        status="pending",
                        owner_id=None,
                        lease_expires_at=None,
                        available_at=now - timedelta(seconds=10),
                        attempt_count=0,
                        last_error=None,
                        payload_json={},
                        context_json={},
                        created_at=now - timedelta(minutes=3),
                        updated_at=now - timedelta(minutes=3),
                        started_at=None,
                        completed_at=None,
                    ),
                    WebhookJob(
                        job_id="job-processing-1",
                        transport="github_webhook",
                        tenant_id="example",
                        project_id="example-default",
                        subject_key="github_pr:example:repo:26",
                        dedupe_key="delivery-2",
                        request_id="request-2",
                        event_type="pull_request_review",
                        status="processing",
                        owner_id="worker:example:webhooks:child:1",
                        lease_expires_at=now + timedelta(minutes=2),
                        available_at=now - timedelta(seconds=5),
                        attempt_count=1,
                        last_error=None,
                        payload_json={},
                        context_json={},
                        created_at=now - timedelta(minutes=2),
                        updated_at=now - timedelta(minutes=1),
                        started_at=now - timedelta(minutes=1),
                        completed_at=None,
                    ),
                    WebhookJob(
                        job_id="job-failed-1",
                        transport="jira_webhook",
                        tenant_id="example",
                        project_id="example-default",
                        subject_key="jira:example:GP-186",
                        dedupe_key="delivery-3",
                        request_id="request-3",
                        event_type="jira:issue_updated",
                        status="failed",
                        owner_id=None,
                        lease_expires_at=None,
                        available_at=now - timedelta(seconds=20),
                        attempt_count=2,
                        last_error="github token missing",
                        payload_json={},
                        context_json={},
                        created_at=now - timedelta(minutes=4),
                        updated_at=now - timedelta(minutes=1),
                        started_at=now - timedelta(minutes=3),
                        completed_at=now - timedelta(minutes=1),
                    ),
                ]
            )
            session.commit()

        response = self.client.get(
            "/api/admin/observability/webhook-jobs?tenant_id=example&project_id=example-default&status=pending&limit=10&offset=0",
            auth=("admin", "secret"),
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["summary"]["pending_count"], 1)
        self.assertEqual(payload["summary"]["processing_count"], 1)
        self.assertEqual(payload["summary"]["failed_count"], 1)
        self.assertEqual(payload["items"][0]["job_id"], "job-pending-1")
        self.assertEqual(payload["items"][0]["status"], "pending")

        missing_project_response = self.client.get(
            "/api/admin/observability/webhook-jobs?tenant_id=example&status=pending&limit=10&offset=0",
            auth=("admin", "secret"),
        )
        self.assertEqual(missing_project_response.status_code, 400)
        self.assertIn("project_id is required", missing_project_response.json()["detail"])

    def test_platform_status_dedupes_legacy_worker_runtime_rows_by_agent_and_mode(self) -> None:
        now = datetime.now(timezone.utc)
        stale = now - timedelta(minutes=10)
        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            session.add(
                WorkerRuntimeState(
                    service_instance_id="host-a:1111",
                    agent_id="worker-linux-local",
                    worker_mode="runs",
                    capabilities_json=["linux"],
                    state="idle",
                    started_at=stale,
                    last_heartbeat_at=stale,
                    updated_at=stale,
                )
            )
            session.add(
                WorkerRuntimeState(
                    service_instance_id="worker-linux-local:runs",
                    agent_id="worker-linux-local",
                    worker_mode="runs",
                    capabilities_json=["linux"],
                    state="busy",
                    started_at=now,
                    last_heartbeat_at=now,
                    updated_at=now,
                )
            )
            add_workflow_attempt(
                session,
                run_id="run-sticky-1",
                tenant_id="example",
                project_id="example-default",
                issue_key="GP-9",
                issue_summary="Sticky worker",
                issue_description=None,
                repo_url=None,
                created_at=now,
                run_status="running",
                workflow_status="running",
                started_at=now,
                last_heartbeat_at=now,
                worker_service_instance_id="worker-linux-local:runs",
            )
            session.commit()

        response = self.client.get("/api/admin/status", auth=("admin", "secret"))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        worker_service = next(service for service in payload["services"] if service["service_id"] == "workers")
        instances = worker_service["instances"]
        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0]["instance_id"], "worker-linux-local:runs")
        self.assertEqual(instances[0]["label"], "worker-linux-local (runs)")
        self.assertEqual(instances[0]["status"], "busy")
        self.assertEqual(instances[0]["active_run_count"], 1)

    def test_admin_platform_status_reports_invalid_worker_capability_configuration(self) -> None:
        with patch.dict(os.environ, {"ORCHESTRATOR_WORKER_CAPABILITIES": "linux,darwin"}, clear=False):
            get_settings.cache_clear()
            response = self.client.get("/api/admin/status", auth=("admin", "secret"))
        get_settings.cache_clear()

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        worker_service = next(service for service in payload["services"] if service["service_id"] == "workers")
        self.assertEqual(worker_service["status"], "degraded")
        self.assertIn("Invalid ORCHESTRATOR_WORKER_CAPABILITIES token(s): darwin", worker_service["summary"])


if __name__ == "__main__":
    unittest.main()
