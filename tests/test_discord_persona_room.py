from __future__ import annotations

import unittest
from unittest.mock import patch

from orchestrator.core.codex_invocation import CodexInvocationContext
from orchestrator.core.codex_runtime import CodexRuntime
from orchestrator.core.discord.persona_room import answer_voice_room_turn
from orchestrator.core.discord.personas import build_voice_room_config, resolve_voice_room_persona_profile


class DiscordPersonaRoomTests(unittest.TestCase):
    def test_build_voice_room_config_merges_tenant_and_project_maps(self) -> None:
        room_config = build_voice_room_config(
            {"persona_names": {"pm": "Ava"}, "persona_voices": {"default": "alloy"}},
            {"persona_names": {"architect": "Soren"}, "persona_voices": {"architect": "echo"}},
        )

        self.assertEqual(room_config["persona_names"]["pm"], "Ava")
        self.assertEqual(room_config["persona_names"]["architect"], "Soren")
        self.assertEqual(room_config["persona_voices"]["default"], "alloy")
        self.assertEqual(room_config["persona_voices"]["architect"], "echo")

    def test_resolve_voice_room_persona_profile_prefers_project_overrides(self) -> None:
        profile = resolve_voice_room_persona_profile(
            persona_id="architect",
            tenant_discord_config={"persona_names": {"architect": "Fallback"}},
            project_discord_config={"persona_names": {"architect": "Soren"}, "persona_voices": {"architect": "echo"}},
        )

        self.assertEqual(profile.persona_id, "architect")
        self.assertEqual(profile.display_name, "Soren")
        self.assertEqual(profile.voice_id, "echo")

    def test_answer_voice_room_turn_routes_and_applies_profile(self) -> None:
        runtime = CodexRuntime(model="gpt-5.4", max_output_tokens=1200, command="override", _request=lambda *_args, **_kwargs: "{}")

        with (
            patch(
                "orchestrator.core.discord.persona_room.route_voice_room_persona_with_codex",
                return_value={"persona": "security", "confidence": 0.87, "reason": "The question is about privacy and permissions."},
            ),
            patch(
                "orchestrator.core.discord.persona_room.answer_voice_room_persona_with_codex",
                return_value={"message": "We need consent, retention limits, and attachment access controls.", "brief": {}},
            ),
        ):
            result = answer_voice_room_turn(
                runtime=runtime,
                transcript="How should we handle private voice notes?",
                project_keys=["MAB"],
                issues=[{"key": "MAB-174"}],
                status_counts={"To Do": 1},
                invocation_context=CodexInvocationContext(
                    channel="discord",
                    tenant_id="tenant-1",
                    project_id="project-1",
                    command="pm",
                    stage="voice-room",
                    working_dir="/tmp/test-repo",
                ),
                history=[{"question": "morning standup", "answer": "starting with product"}],
                github_context={"repository": "repo"},
                tenant_discord_config={"persona_names": {"security": "June"}},
                project_discord_config={"persona_voices": {"security": "secure-voice"}},
            )

        self.assertEqual(result.persona_id, "security")
        self.assertEqual(result.persona_name, "June")
        self.assertEqual(result.persona_voice_id, "secure-voice")
        self.assertEqual(result.router_confidence, 0.87)
        self.assertIn("consent", result.message)


if __name__ == "__main__":
    unittest.main()
