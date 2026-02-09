import unittest

from orchestrator.api.discord_response_format import (
    build_issue_url_list,
    build_jira_issue_url,
    format_issue_markdown_link,
    format_issue_markdown_list,
    normalize_browse_base_url,
)


class DiscordResponseFormatTests(unittest.TestCase):
    def test_normalize_browse_base_url_strips_and_trims_slash(self) -> None:
        self.assertEqual(normalize_browse_base_url(" https://jira.example.com/ "), "https://jira.example.com")
        self.assertEqual(normalize_browse_base_url(None), "")

    def test_build_jira_issue_url_returns_none_without_base(self) -> None:
        self.assertIsNone(build_jira_issue_url(issue_key="TP-1", browse_base_url=None))

    def test_build_jira_issue_url_builds_browse_link(self) -> None:
        self.assertEqual(
            build_jira_issue_url(issue_key="TP-1", browse_base_url="https://jira.example.com/"),
            "https://jira.example.com/browse/TP-1",
        )

    def test_format_issue_markdown_link_falls_back_to_key(self) -> None:
        self.assertEqual(format_issue_markdown_link(issue_key="TP-1", browse_base_url=""), "TP-1")

    def test_format_issue_markdown_link_formats_markdown(self) -> None:
        self.assertEqual(
            format_issue_markdown_link(issue_key="TP-1", browse_base_url="https://jira.example.com"),
            "[TP-1](https://jira.example.com/browse/TP-1)",
        )

    def test_format_issue_markdown_list_formats_all_or_none(self) -> None:
        self.assertEqual(format_issue_markdown_list(issue_keys=[], browse_base_url="https://jira.example.com"), "none")
        self.assertEqual(
            format_issue_markdown_list(issue_keys=["TP-1", "TP-2"], browse_base_url="https://jira.example.com"),
            "[TP-1](https://jira.example.com/browse/TP-1), [TP-2](https://jira.example.com/browse/TP-2)",
        )

    def test_build_issue_url_list_omits_empty_base(self) -> None:
        self.assertEqual(build_issue_url_list(issue_keys=["TP-1", "TP-2"], browse_base_url=None), [])
        self.assertEqual(
            build_issue_url_list(issue_keys=["TP-1", "TP-2"], browse_base_url="https://jira.example.com/"),
            [
                "https://jira.example.com/browse/TP-1",
                "https://jira.example.com/browse/TP-2",
            ],
        )


if __name__ == "__main__":
    unittest.main()
