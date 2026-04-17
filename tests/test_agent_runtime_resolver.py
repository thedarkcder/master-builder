from __future__ import annotations

import unittest
from types import SimpleNamespace

from orchestrator.core.agent_runtime_resolver import _resolve_agent_execution_profiles
from orchestrator.core.config import Settings


class AgentRuntimeResolverTests(unittest.TestCase):
    def test_default_engineering_profile_does_not_depend_on_global_codex_model_setting(self) -> None:
        settings = SimpleNamespace(
            codex_cli_command="codex",
            codex_reasoning_effort="high",
            codex_supported_models="gpt-5.4,gpt-5.4-mini,gpt-5.3-codex",
            chat_cli_command="chat-cli",
            chat_model="gpt-5.4",
            chat_reasoning_effort="medium",
            claude_cli_command="claude",
        )

        profile, _profiles = _resolve_agent_execution_profiles(
            settings=settings,
            tenant_policy={},
            project_overrides={},
            selector="workflow.dev",
            platform_selector_routing={},
            platform_profiles={},
        )

        self.assertEqual(profile.profile_name, "engineering_execution")
        self.assertEqual(profile.runtime_kind, "codex_cli")
        self.assertEqual(profile.model, "gpt-5.4")
        self.assertEqual(profile.reasoning_effort, "high")

    def test_platform_general_planning_profile_preserves_custom_model_for_retro_selector(self) -> None:
        settings = Settings(
            codex_reasoning_effort="high",
        )
        platform_profiles = {
            "general_planning": {
                "runtime_kind": "lm_studio",
                "cli_command": "",
                "model": "openai/gpt-oss-20b",
                "reasoning_effort": "high",
                "tool_bridge_allowed": True,
                "base_url": "http://localhost:1234/v1",
            }
        }

        profile, _profiles = _resolve_agent_execution_profiles(
            settings=settings,
            tenant_policy={},
            project_overrides={},
            selector="workflow.retro_voice_brief",
            platform_selector_routing={},
            platform_profiles=platform_profiles,
        )

        self.assertEqual(profile.runtime_kind, "lm_studio")
        self.assertEqual(profile.model, "openai/gpt-oss-20b")
        self.assertEqual(profile.reasoning_effort, "high")

    def test_platform_general_planning_profile_preserves_custom_model_for_standup_selector(self) -> None:
        settings = Settings(
            codex_reasoning_effort="high",
        )
        platform_profiles = {
            "general_planning": {
                "runtime_kind": "lm_studio",
                "cli_command": "",
                "model": "openai/gpt-oss-20b",
                "reasoning_effort": "high",
                "tool_bridge_allowed": True,
                "base_url": "http://localhost:1234/v1",
            }
        }

        profile, _profiles = _resolve_agent_execution_profiles(
            settings=settings,
            tenant_policy={},
            project_overrides={},
            selector="workflow.standup_voice_brief",
            platform_selector_routing={},
            platform_profiles=platform_profiles,
        )

        self.assertEqual(profile.runtime_kind, "lm_studio")
        self.assertEqual(profile.model, "openai/gpt-oss-20b")
        self.assertEqual(profile.reasoning_effort, "high")


if __name__ == "__main__":
    unittest.main()
