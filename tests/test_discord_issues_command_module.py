import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi import HTTPException

from orchestrator.api.discord.commands.issues import dispatch_issues_command
from orchestrator.api.schemas import DiscordCommandRequest


class DiscordIssuesCommandDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = MagicMock()
        self.tenant = SimpleNamespace(tenant_id="tenant-a")
        self.payload = DiscordCommandRequest(user_id="u-1", command="!issues", channel_id="c-1")

    def _call(self, **overrides):
        params = {
            "session": self.session,
            "tenant": self.tenant,
            "payload": self.payload,
            "command_name": "issues",
            "arguments": [],
            "scoped_project_keys": [],
            "codex_working_dir": "/tmp",
            "normalized_user_id": "u-1",
            "defer_seed_issues": False,
            "seed_issues_with_codex": MagicMock(return_value=("ok", {"requires_input": False})),
            "find_seed_followup_context": MagicMock(return_value=None),
            "store_seed_followup_context": MagicMock(return_value="req-1"),
            "clear_seed_followup_context": MagicMock(),
            "validate_seed_followup_context": None,
        }
        params.update(overrides)
        return dispatch_issues_command(**params)

    def test_non_issues_command_returns_none(self) -> None:
        response = self._call(command_name="ask")
        self.assertIsNone(response)

    def test_missing_arguments_returns_usage_error(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            self._call(arguments=[])
        self.assertEqual(ctx.exception.status_code, 400)

    def test_seed_deferred_returns_deferred_payload(self) -> None:
        response = self._call(arguments=["seed", "spec"], defer_seed_issues=True)
        self.assertEqual(response.data["deferred"], True)

    def test_seed_passes_scoped_project_keys(self) -> None:
        seed_mock = MagicMock(return_value=("ok", {"requires_input": False}))
        response = self._call(
            arguments=["seed", "spec"],
            scoped_project_keys=["GP"],
            seed_issues_with_codex=seed_mock,
        )
        self.assertEqual(response.message, "ok")
        self.assertEqual(seed_mock.call_args.kwargs["scoped_project_keys"], ["GP"])

    def test_followup_requires_channel_context(self) -> None:
        payload = DiscordCommandRequest(user_id="u-1", command="!issues")
        with self.assertRaises(HTTPException) as ctx:
            self._call(payload=payload, arguments=["followup", "answer"])
        self.assertEqual(ctx.exception.status_code, 400)

    def test_followup_requires_existing_context(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            self._call(arguments=["followup", "answer"], find_seed_followup_context=MagicMock(return_value=None))
        self.assertEqual(ctx.exception.status_code, 409)

    def test_followup_blocks_different_user(self) -> None:
        context = {"request_id": "req-1", "user_id": "someone-else", "channel_ids": ["c-1"]}
        with self.assertRaises(HTTPException) as ctx:
            self._call(
                arguments=["followup", "answer"],
                find_seed_followup_context=MagicMock(return_value=context),
            )
        self.assertEqual(ctx.exception.status_code, 403)

    def test_followup_success_clears_context_when_no_more_input_required(self) -> None:
        clear_context = MagicMock()
        context = {
            "request_id": "req-1",
            "user_id": "u-1",
            "channel_ids": ["c-1"],
            "prompt_markdown": "seed spec",
            "issue_keys": ["TP-1"],
            "questions": ["Question?"],
        }
        response = self._call(
            arguments=["followup", "answer text"],
            find_seed_followup_context=MagicMock(return_value=context),
            seed_issues_with_codex=MagicMock(return_value=("updated", {"requires_input": False})),
            clear_seed_followup_context=clear_context,
        )
        self.assertEqual(response.message, "updated")
        clear_context.assert_called_once()

    def test_followup_invalid_context_is_cleared_and_rejected(self) -> None:
        clear_context = MagicMock()
        context = {
            "request_id": "req-1",
            "user_id": "u-1",
            "channel_ids": ["c-1"],
            "prompt_markdown": "seed spec",
            "issue_keys": ["TP-1"],
            "questions": ["Question?"],
        }
        with self.assertRaises(HTTPException) as ctx:
            self._call(
                arguments=["followup", "answer text"],
                find_seed_followup_context=MagicMock(return_value=context),
                validate_seed_followup_context=MagicMock(return_value=(False, "referenced Jira issues no longer exist")),
                clear_seed_followup_context=clear_context,
            )
        self.assertEqual(ctx.exception.status_code, 409)
        clear_context.assert_called_once()
