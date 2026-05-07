from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from orchestrator.api.atlassian_oauth.connection_service import resolve_tenant_atlassian_connection, tenant_atlassian_oauth_context
from orchestrator.core.runtime.agent_runtime_resolver import resolve_agent_execution_profile
from orchestrator.core.projects import policy as project_policy
from orchestrator.core.communications import integration_contracts


class ProjectPolicyHelpersTests(unittest.TestCase):
    def test_normalize_project_policy_overrides(self) -> None:
        normalized = project_policy.normalize_project_policy_overrides(
            {
                "allow_jira_transitions": True,
                "allow_pr_creation": False,
                "allow_code_reviews": True,
                "allow_pr_remediation": False,
                "allow_manual_pr_fix_requests": False,
                "allow_label_mutations": "nope",
                "allow_auto_merge": True,
                "max_dev_test_review_loops": 0,
                "max_pr_auto_remediation_loops": 0,
                "max_concurrent_runs": "bad",
                "allowed_commands": [" run ", "", "  ", "retry"],
                "require_agents_md": True,
                "codex_model": " gpt-5.4-mini ",
                "codex_reasoning_effort": " high ",
                "ignored": "x",
            }
        )
        self.assertEqual(normalized["allow_jira_transitions"], True)
        self.assertEqual(normalized["allow_pr_creation"], False)
        self.assertEqual(normalized["allow_code_reviews"], True)
        self.assertEqual(normalized["allow_pr_remediation"], False)
        self.assertEqual(normalized["allow_manual_pr_fix_requests"], False)
        self.assertEqual(normalized["allow_auto_merge"], True)
        self.assertNotIn("allow_label_mutations", normalized)
        self.assertEqual(normalized["max_dev_test_review_loops"], 1)
        self.assertEqual(normalized["max_pr_auto_remediation_loops"], 1)
        self.assertNotIn("max_concurrent_runs", normalized)
        self.assertEqual(normalized["allowed_commands"], ["run", "retry"])
        self.assertEqual(normalized["require_agents_md"], True)
        self.assertEqual(normalized["codex_model"], "gpt-5.4-mini")
        self.assertEqual(normalized["codex_reasoning_effort"], "high")

    def test_normalize_project_policy_overrides_includes_execution_profiles(self) -> None:
        normalized = project_policy.normalize_project_policy_overrides(
            {
                "execution_profiles": {
                    "pm_conversation": {
                        "runtime_kind": "chat_cli",
                        "cli_command": "chat-cli",
                        "model": "gpt-5.4",
                        "reasoning_effort": "medium",
                        "tool_bridge_allowed": False,
                        "fallback_profile": "general_planning",
                    }
                },
                "execution_profile_routing": {
                    "discord.pm_answer": "pm_conversation",
                },
            }
        )
        self.assertEqual(normalized["execution_profiles"]["pm_conversation"]["cli_command"], "chat-cli")
        self.assertEqual(normalized["execution_profile_routing"]["discord.pm_answer"], "pm_conversation")

    def test_resolve_effective_policy_caps_and_intersections(self) -> None:
        effective = project_policy.resolve_effective_policy(
            tenant_policy={
                "allow_jira_transitions": True,
                "allow_pr_creation": True,
                "allow_code_reviews": True,
                "allow_pr_remediation": True,
                "allow_manual_pr_fix_requests": True,
                "allow_label_mutations": True,
                "allow_auto_merge": True,
                "max_dev_test_review_loops": 10,
                "max_pr_auto_remediation_loops": 5,
                "max_concurrent_runs": 8,
                "allowed_commands": ["run", "retry"],
                "require_agents_md": False,
                "codex_model": "gpt-5.4",
                "codex_reasoning_effort": "medium",
            },
            project_overrides={
                "allow_pr_creation": False,
                "allow_code_reviews": False,
                "allow_pr_remediation": False,
                "allow_manual_pr_fix_requests": False,
                "allow_auto_merge": False,
                "max_dev_test_review_loops": 999,
                "max_pr_auto_remediation_loops": 999,
                "max_concurrent_runs": 3,
                "allowed_commands": ["retry", "cancel"],
                "require_agents_md": True,
                "codex_model": "gpt-5.4-mini",
                "codex_reasoning_effort": "high",
            },
            default_codex_model="gpt-5.4",
            default_codex_reasoning_effort="medium",
        )
        self.assertEqual(effective["allow_pr_creation"], False)
        self.assertEqual(effective["allow_code_reviews"], False)
        self.assertEqual(effective["allow_pr_remediation"], False)
        self.assertEqual(effective["allow_manual_pr_fix_requests"], False)
        self.assertEqual(effective["allow_auto_merge"], False)
        self.assertEqual(effective["max_dev_test_review_loops"], 10)
        self.assertEqual(effective["max_pr_auto_remediation_loops"], 5)
        self.assertEqual(effective["max_concurrent_runs"], 3)
        self.assertEqual(effective["allowed_commands"], ["retry"])
        self.assertEqual(effective["require_agents_md"], True)
        self.assertEqual(effective["codex_model"], "gpt-5.4-mini")
        self.assertEqual(effective["codex_reasoning_effort"], "high")

    def test_resolve_agent_execution_profile_prefers_selector_specific_profile(self) -> None:
        settings = SimpleNamespace(
            codex_cli_command="codex",
            codex_model="gpt-5.3-codex",
            codex_reasoning_effort="high",
            chat_cli_command="chat-cli",
            chat_model="gpt-5.4",
            chat_reasoning_effort="medium",
        )
        profile = resolve_agent_execution_profile(
            settings=settings,
            tenant_policy={
                "execution_profiles": {
                    "pm_conversation": {
                        "runtime_kind": "chat_cli",
                        "cli_command": "chat-cli",
                        "model": "gpt-5.4",
                        "reasoning_effort": "medium",
                        "tool_bridge_allowed": False,
                        "fallback_profile": "general_planning",
                    }
                },
                "execution_profile_routing": {
                    "discord.pm_answer": "pm_conversation",
                },
            },
            project_overrides=None,
            selector="discord.pm_answer",
        )
        self.assertEqual(profile.profile_name, "pm_conversation")
        self.assertEqual(profile.runtime_kind, "chat_cli")
        self.assertEqual(profile.cli_command, "chat-cli")
        self.assertEqual(profile.model, "gpt-5.4")
        self.assertFalse(profile.tool_bridge_allowed)

    def test_resolve_agent_execution_profile_preserves_explicit_engineering_profile(self) -> None:
        settings = SimpleNamespace(
            codex_cli_command="codex",
            codex_model="gpt-5.3-codex",
            codex_reasoning_effort="high",
            chat_cli_command="chat-cli",
            chat_model="gpt-5.4",
            chat_reasoning_effort="medium",
        )
        profile = resolve_agent_execution_profile(
            settings=settings,
            tenant_policy={
                "codex_model": "gpt-5.4",
                "codex_reasoning_effort": "medium",
                "execution_profiles": {
                    "engineering_execution": {
                        "runtime_kind": "codex_cli",
                        "cli_command": "codex-alt",
                        "model": "gpt-5.4-mini",
                        "reasoning_effort": "low",
                        "tool_bridge_allowed": True,
                    }
                },
            },
            project_overrides=None,
            selector="workflow.dev",
        )
        self.assertEqual(profile.profile_name, "engineering_execution")
        self.assertEqual(profile.cli_command, "codex-alt")
        self.assertEqual(profile.model, "gpt-5.4-mini")
        self.assertEqual(profile.reasoning_effort, "low")

    def test_resolve_agent_execution_profile_prefers_platform_named_agent_routing(self) -> None:
        settings = SimpleNamespace(
            codex_cli_command="codex",
            codex_model="gpt-5.4",
            codex_reasoning_effort="medium",
            codex_supported_models="gpt-5.4,gpt-5.4-mini,gpt-5.3-codex",
            chat_cli_command="chat-cli",
            chat_model="gpt-5.4",
            chat_reasoning_effort="medium",
        )
        profile = resolve_agent_execution_profile(
            settings=settings,
            tenant_policy={
                "execution_profile_routing": {
                    "discord.pm_answer": "pm_conversation",
                },
            },
            project_overrides=None,
            selector="discord.pm_answer",
            agent_role="pm",
            agent_name="voice_room_pm",
            platform_role_routing={"pm": "pm_conversation_default"},
            platform_name_routing={"voice_room_pm": "pm_conversation_fast"},
        )
        self.assertEqual(profile.profile_name, "pm_conversation_fast")
        self.assertEqual(profile.runtime_kind, "chat_cli")
        self.assertEqual(profile.reasoning_effort, "low")

    def test_resolve_agent_execution_profile_prefers_platform_role_over_selector(self) -> None:
        settings = SimpleNamespace(
            codex_cli_command="codex",
            codex_model="gpt-5.4",
            codex_reasoning_effort="medium",
            codex_supported_models="gpt-5.4,gpt-5.4-mini,gpt-5.3-codex",
            chat_cli_command="chat-cli",
            chat_model="gpt-5.4",
            chat_reasoning_effort="medium",
        )
        profile = resolve_agent_execution_profile(
            settings=settings,
            tenant_policy={
                "execution_profile_routing": {
                    "workflow.dev": "general_planning",
                },
            },
            project_overrides=None,
            selector="workflow.dev",
            agent_role="engineering",
            agent_name=None,
            platform_role_routing={"engineering": "engineering_execution_deep"},
            platform_name_routing={},
        )
        self.assertEqual(profile.profile_name, "engineering_execution_deep")
        self.assertEqual(profile.runtime_kind, "codex_cli")
        self.assertEqual(profile.reasoning_effort, "high")


class JiraConnectionServiceTests(unittest.TestCase):
    def test_resolve_tenant_atlassian_connection_validation(self) -> None:
        session = MagicMock()
        tenant = SimpleNamespace(jira_config={})
        with self.assertRaises(HTTPException) as missing_ctx:
            resolve_tenant_atlassian_connection(session=session, tenant=tenant)
        self.assertEqual(missing_ctx.exception.status_code, 400)

        tenant = SimpleNamespace(jira_config={"connection_id": "conn-1"})
        session.get.return_value = None
        with self.assertRaises(HTTPException) as not_found_ctx:
            resolve_tenant_atlassian_connection(session=session, tenant=tenant)
        self.assertEqual(not_found_ctx.exception.status_code, 400)

    def test_tenant_atlassian_oauth_context(self) -> None:
        session = MagicMock()
        tenant = SimpleNamespace(jira_config={"connection_id": "conn-1"})
        connection = SimpleNamespace(connection_id="conn-1")
        session.get.return_value = connection
        settings = SimpleNamespace()

        with (
            patch("orchestrator.api.atlassian_oauth.connection_service.refresh_atlassian_connection_tokens", return_value="tok"),
            patch("orchestrator.api.atlassian_oauth.connection_service.atlassian_oauth_client", return_value="client"),
        ):
            context = tenant_atlassian_oauth_context(session=session, tenant=tenant, settings=settings)

        self.assertEqual(context.connection, connection)
        self.assertEqual(context.access_token, "tok")
        self.assertEqual(context.client, "client")


class IntegrationContractsTests(unittest.TestCase):
    def test_protocol_default_bodies_execute(self) -> None:
        # These calls exercise protocol method bodies so they are covered.
        self.assertIsNone(integration_contracts.InboundAdapter.verify(object(), headers={}, body=b""))
        self.assertIsNone(integration_contracts.InboundAdapter.parse(object(), headers={}, body=b""))
        self.assertIsNone(integration_contracts.OutboundAdapter.send(object(), event=object()))
        self.assertIsNone(integration_contracts.TransportActionExecutor.execute(object(), action=object()))


if __name__ == "__main__":
    unittest.main()
