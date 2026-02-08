import unittest

from orchestrator.core.discord_commands_sync import build_discord_guild_commands


class DiscordCommandSyncTests(unittest.TestCase):
    def test_build_commands_includes_ask_and_issues_seed(self) -> None:
        commands = build_discord_guild_commands()
        command_names = {command.get("name") for command in commands}
        self.assertIn("ask", command_names)
        self.assertIn("issues", command_names)
        self.assertIn("request", command_names)

        issues_command = next(command for command in commands if command.get("name") == "issues")
        options = issues_command.get("options")
        self.assertIsInstance(options, list)
        seed_option = next(option for option in options if option.get("name") == "seed")
        self.assertEqual(seed_option.get("type"), 1)

        ask_command = next(command for command in commands if command.get("name") == "ask")
        ask_options = ask_command.get("options")
        self.assertIsInstance(ask_options, list)
        self.assertGreaterEqual(len(ask_options), 2)
        self.assertEqual(ask_options[0].get("name"), "question")
        self.assertEqual(ask_options[0].get("required"), True)
        self.assertEqual(ask_options[1].get("name"), "issue_key")
        self.assertEqual(ask_options[1].get("required"), False)
        issue_key_option = next(option for option in ask_options if option.get("name") == "issue_key")
        self.assertEqual(issue_key_option.get("autocomplete"), True)


if __name__ == "__main__":
    unittest.main()
