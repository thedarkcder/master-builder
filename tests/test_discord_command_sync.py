import unittest

from orchestrator.core.discord_commands_sync import build_discord_guild_commands


class DiscordCommandSyncTests(unittest.TestCase):
    def test_build_commands_includes_ask(self) -> None:
        commands = build_discord_guild_commands()
        command_names = {command.get("name") for command in commands}
        self.assertIn("ask", command_names)


if __name__ == "__main__":
    unittest.main()
