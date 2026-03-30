from __future__ import annotations

import unittest

from orchestrator.core.agent_runtime_resolver import _resolve_agent_execution_profiles
from orchestrator.core.config import Settings


class AgentRuntimeResolverTests(unittest.TestCase):
    def test_platform_general_planning_profile_preserves_custom_model_for_retro_selector(self) -> None:
        settings = Settings(
            codex_model="gpt-5.4",
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
            codex_model="gpt-5.4",
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
