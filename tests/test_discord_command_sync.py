import unittest

from orchestrator.core.discord_commands_sync import build_discord_guild_commands


class DiscordCommandSyncTests(unittest.TestCase):
    def test_build_commands_includes_ask_and_issues_seed(self) -> None:
        commands = build_discord_guild_commands()
        command_names = {command.get("name") for command in commands}
        self.assertIn("ask", command_names)
        self.assertIn("issues", command_names)

        issues_command = next(command for command in commands if command.get("name") == "issues")
        options = issues_command.get("options")
        self.assertIsInstance(options, list)
        seed_option = next(option for option in options if option.get("name") == "seed")
        self.assertEqual(seed_option.get("type"), 1)

        ask_command = next(command for command in commands if command.get("name") == "ask")
        ask_options = ask_command.get("options")
        self.assertIsInstance(ask_options, list)
        issue_key_option = next(option for option in ask_options if option.get("name") == "issue_key")
        self.assertEqual(issue_key_option.get("autocomplete"), True)


if __name__ == "__main__":
    unittest.main()
