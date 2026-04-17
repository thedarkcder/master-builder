import os

import pytest

from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import PlatformSetting
from tests.test_support.admin_api_harness import AdminApiTestHarness


pytestmark = pytest.mark.contract


class AdminRuntimeAndSecretApiTests(AdminApiTestHarness):
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
        self.assertEqual([item["id"] for item in body["models"]], ["gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex"])
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
