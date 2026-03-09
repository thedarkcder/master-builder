from __future__ import annotations

import unittest

from orchestrator.core.discord.thread_context import (
    LEGACY_THREAD_ISSUE_BY_CHANNEL_ID_KEY,
    THREAD_ISSUE_BY_CHANNEL_ID_KEY,
    get_thread_issue_key,
    normalize_issue_key,
    put_thread_issue_key,
)


class DiscordThreadContextTests(unittest.TestCase):
    def test_normalize_issue_key(self) -> None:
        self.assertEqual(normalize_issue_key(" mab-1 "), "MAB-1")
        self.assertEqual(normalize_issue_key("invalid"), "")

    def test_put_and_get_thread_issue_key_roundtrip(self) -> None:
        config = put_thread_issue_key(discord_config={}, channel_id="thread-1", issue_key="mab-10")
        self.assertEqual(config[THREAD_ISSUE_BY_CHANNEL_ID_KEY]["thread-1"], "MAB-10")
        self.assertEqual(config[LEGACY_THREAD_ISSUE_BY_CHANNEL_ID_KEY]["thread-1"], "MAB-10")
        self.assertEqual(get_thread_issue_key(discord_config=config, channel_id="thread-1"), "MAB-10")

    def test_get_thread_issue_key_supports_legacy_only(self) -> None:
        config = {LEGACY_THREAD_ISSUE_BY_CHANNEL_ID_KEY: {"thread-2": "MAB-11"}}
        self.assertEqual(get_thread_issue_key(discord_config=config, channel_id="thread-2"), "MAB-11")

    def test_get_thread_issue_key_prefers_new_mapping_over_legacy(self) -> None:
        config = {
            THREAD_ISSUE_BY_CHANNEL_ID_KEY: {"thread-9": "MAB-90"},
            LEGACY_THREAD_ISSUE_BY_CHANNEL_ID_KEY: {"thread-9": "MAB-91"},
        }
        self.assertEqual(get_thread_issue_key(discord_config=config, channel_id="thread-9"), "MAB-90")


if __name__ == "__main__":
    unittest.main()
