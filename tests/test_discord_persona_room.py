from __future__ import annotations

import unittest

from orchestrator.core.discord.personas import (
    build_voice_room_config,
    build_voice_room_spoken_reply_text,
    format_voice_room_persona_label,
    resolve_voice_room_persona_profile,
)


class DiscordPersonaRoomTests(unittest.TestCase):
    def test_build_voice_room_config_merges_tenant_and_project_maps(self) -> None:
        room_config = build_voice_room_config(
            {"persona_names": {"pm": "Ava"}, "persona_voices": {"default": "alba"}},
            {
                "persona_names": {"architect": "Soren"},
                "persona_voices": {"architect": "javert"},
            },
        )

        self.assertEqual(room_config["persona_names"]["pm"], "Ava")
        self.assertEqual(room_config["persona_names"]["architect"], "Soren")
        self.assertEqual(room_config["persona_voices"]["default"], "alba")
        self.assertEqual(room_config["persona_voices"]["architect"], "javert")

    def test_resolve_voice_room_persona_profile_prefers_project_overrides(self) -> None:
        profile = resolve_voice_room_persona_profile(
            persona_id="architect",
            tenant_discord_config={"persona_names": {"architect": "Fallback"}},
            project_discord_config={
                "persona_names": {"architect": "Soren"},
                "persona_voices": {"architect": "javert"},
            },
        )

        self.assertEqual(profile.persona_id, "architect")
        self.assertEqual(profile.display_name, "Soren")
        self.assertEqual(profile.voice_id, "javert")

    def test_persona_label_and_spoken_reply_use_name_from_role(self) -> None:
        self.assertEqual(
            format_voice_room_persona_label(
                persona_id="pm",
                persona_name="Andy",
                persona_role="PM",
            ),
            "Andy from Product",
        )
        self.assertEqual(
            format_voice_room_persona_label(
                persona_id="pm",
                persona_name="PM",
                persona_role="PM",
            ),
            "Andy from Product",
        )
        self.assertEqual(
            build_voice_room_spoken_reply_text(
                message="We should cut scope.",
                persona_id="engineer",
                persona_name="Bill",
                persona_role="Engineer",
            ),
            "Bill from Engineering. We should cut scope.",
        )


if __name__ == "__main__":
    unittest.main()
