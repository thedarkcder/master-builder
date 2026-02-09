import re
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi import HTTPException

from orchestrator.api.discord_command_bug_gap import dispatch_bug_gap_command
from orchestrator.api.schemas import DiscordCommandRequest


class DiscordCommandBugGapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = object()
        self.tenant = SimpleNamespace(tenant_id="tenant-1")
        self.issue_key_pattern = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")

    def _dispatch(
        self,
        *,
        payload: DiscordCommandRequest,
        command_name: str,
        arguments: list[str],
        run_gap_analysis: Mock | None = None,
        normalize_discord_attachments: Mock | None = None,
        create_discord_bug_issue: Mock | None = None,
    ):
        return dispatch_bug_gap_command(
            session=self.session,
            tenant=self.tenant,
            payload=payload,
            command_name=command_name,
            arguments=arguments,
            issue_key_pattern=self.issue_key_pattern,
            run_gap_analysis=run_gap_analysis or Mock(),
            normalize_discord_attachments=normalize_discord_attachments or Mock(return_value=[]),
            create_discord_bug_issue=create_discord_bug_issue or Mock(return_value=("ok", {"created_issue_keys": []})),
        )

    def test_returns_none_for_non_bug_gap_commands(self) -> None:
        response = self._dispatch(
            payload=DiscordCommandRequest(user_id="u1", channel_id="c1", command="!help"),
            command_name="help",
            arguments=[],
        )
        self.assertIsNone(response)

    def test_gap_requires_issue_key(self) -> None:
        with self.assertRaises(HTTPException) as exc:
            self._dispatch(
                payload=DiscordCommandRequest(user_id="u1", channel_id="c1", command="!gap"),
                command_name="gap",
                arguments=[],
            )
        self.assertEqual(exc.exception.status_code, 400)
        self.assertIn("Usage: !gap", str(exc.exception.detail))

    def test_gap_returns_analysis(self) -> None:
        run_gap_analysis = Mock(return_value=("Gap body", {"issue_key": "TP-77"}))
        response = self._dispatch(
            payload=DiscordCommandRequest(user_id="u1", channel_id="c1", command="!gap TP-77"),
            command_name="gap",
            arguments=["TP-77"],
            run_gap_analysis=run_gap_analysis,
        )

        self.assertIsNotNone(response)
        self.assertTrue(response.ok)
        self.assertEqual(response.command, "gap")
        self.assertEqual(response.data["issue_key"], "TP-77")
        run_gap_analysis.assert_called_once()
        self.assertEqual(run_gap_analysis.call_args.kwargs["issue_key"], "TP-77")

    def test_bug_requires_summary(self) -> None:
        with self.assertRaises(HTTPException) as exc:
            self._dispatch(
                payload=DiscordCommandRequest(user_id="u1", channel_id="c1", command="!bug"),
                command_name="bug",
                arguments=[],
            )
        self.assertEqual(exc.exception.status_code, 400)
        self.assertIn("Usage: !bug", str(exc.exception.detail))

    def test_bug_uses_command_params_and_attachments(self) -> None:
        normalize_attachments = Mock(return_value=[{"filename": "screenshot.png", "url": "https://example.com/a.png"}])
        create_bug = Mock(
            return_value=("Bug logged: TP-501", {"created_issue_keys": ["TP-501"]})
        )
        response = self._dispatch(
            payload=DiscordCommandRequest(
                user_id="u1",
                channel_id="c1",
                command="!bug",
                command_params={
                    "summary": "Login fails",
                    "details": "Spinner never ends",
                    "issue_key": "TP-7",
                },
                attachments=[{"filename": "screenshot.png", "url": "https://example.com/a.png"}],
            ),
            command_name="bug",
            arguments=[],
            normalize_discord_attachments=normalize_attachments,
            create_discord_bug_issue=create_bug,
        )

        self.assertIsNotNone(response)
        self.assertTrue(response.ok)
        self.assertEqual(response.command, "bug")
        create_bug.assert_called_once()
        kwargs = create_bug.call_args.kwargs
        self.assertEqual(kwargs["summary"], "Login fails")
        self.assertEqual(kwargs["details"], "Spinner never ends")
        self.assertEqual(kwargs["related_issue_key"], "TP-7")
        self.assertEqual(kwargs["attachments"][0]["filename"], "screenshot.png")

    def test_bug_parses_summary_and_details_from_arguments(self) -> None:
        create_bug = Mock(
            return_value=("Bug logged: TP-502", {"created_issue_keys": ["TP-502"]})
        )
        response = self._dispatch(
            payload=DiscordCommandRequest(user_id="u1", channel_id="c1", command="!bug login -- details"),
            command_name="bug",
            arguments=["login", "--", "details"],
            create_discord_bug_issue=create_bug,
        )

        self.assertIsNotNone(response)
        self.assertTrue(response.ok)
        kwargs = create_bug.call_args.kwargs
        self.assertEqual(kwargs["summary"], "login")
        self.assertEqual(kwargs["details"], "details")


if __name__ == "__main__":
    unittest.main()
