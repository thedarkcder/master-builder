from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from orchestrator.api.discord.commands import dispatcher
from orchestrator.core.communications.command_pipeline import CommandScope


class _ExecResult:
    def __init__(self, *, scalar_one=None, scalar_one_or_none=None, items=None):  # noqa: ANN001
        self._scalar_one = scalar_one
        self._scalar_one_or_none = scalar_one_or_none
        self._items = items or []

    def scalar_one(self):  # noqa: ANN201
        return self._scalar_one

    def scalar_one_or_none(self):  # noqa: ANN201
        return self._scalar_one_or_none

    def scalars(self):  # noqa: ANN201
        return self

    def all(self):  # noqa: ANN201
        return self._items


class DiscordCommandDispatcherModuleTests(unittest.TestCase):
    def test_issue_project_key_and_scope_assertion(self) -> None:
        self.assertEqual(dispatcher._issue_project_key("mab-1"), "MAB")
        self.assertEqual(dispatcher._issue_project_key("mab"), "")

        dispatcher._assert_issue_key_in_scope(issue_key="MAB-1", scope=CommandScope(project_id=None, project_keys=[]))
        dispatcher._assert_issue_key_in_scope(issue_key="MAB-1", scope=CommandScope(project_id=None, project_keys=[" MAB "]))

        with self.assertRaises(HTTPException) as ctx:
            dispatcher._assert_issue_key_in_scope(issue_key="APP-1", scope=CommandScope(project_id=None, project_keys=["MAB"]))
        self.assertEqual(ctx.exception.status_code, 403)

    def test_format_elapsed_seconds(self) -> None:
        self.assertEqual(dispatcher._format_elapsed_seconds(started_at=None, created_at=None), 0)
        naive = datetime.now().replace(microsecond=0)
        self.assertGreaterEqual(dispatcher._format_elapsed_seconds(started_at=naive, created_at=None), 0)

    def test_dispatch_help_policy_and_status(self) -> None:
        session = MagicMock()
        session.execute.side_effect = [
            _ExecResult(scalar_one=2),
            _ExecResult(items=[SimpleNamespace(run_id="r1", issue_key="MAB-1", status="running", started_at=None, created_at=None)]),
        ]
        tenant = SimpleNamespace(is_enabled=True)
        payload = SimpleNamespace(user_id="u1", channel_id="c1")

        help_result = dispatcher.dispatch_simple_discord_command(
            session=session,
            tenant=tenant,
            tenant_id="route25",
            payload=payload,
            command_name="help",
            arguments=[],
            jira_browse_base_url=None,
            scope=CommandScope(project_id=None, project_keys=[]),
        )
        self.assertIsNotNone(help_result)
        assert help_result is not None
        self.assertIn("!run", help_result.message)

        policy_result = dispatcher.dispatch_simple_discord_command(
            session=session,
            tenant=tenant,
            tenant_id="route25",
            payload=payload,
            command_name="policy",
            arguments=[],
            jira_browse_base_url=None,
            scope=CommandScope(project_id=None, project_keys=[]),
        )
        self.assertIsNotNone(policy_result)
        assert policy_result is not None
        self.assertIn("Default policy", policy_result.message)

        status_result = dispatcher.dispatch_simple_discord_command(
            session=session,
            tenant=tenant,
            tenant_id="route25",
            payload=payload,
            command_name="status",
            arguments=[],
            jira_browse_base_url=None,
            scope=CommandScope(project_id="route25-default", project_keys=["MAB"]),
        )
        self.assertIsNotNone(status_result)
        assert status_result is not None
        self.assertEqual(status_result.data["queue_depth"], 2)
        self.assertEqual(len(status_result.data["active_runs"]), 1)

    def test_dispatch_runs_and_link_and_request(self) -> None:
        session = MagicMock()
        tenant = SimpleNamespace(is_enabled=True, tenant_id="route25")
        payload = SimpleNamespace(user_id=" user-1 ", channel_id=" c1 ")
        scope = CommandScope(project_id="route25-default", project_keys=["MAB"])

        session.execute.side_effect = [
            _ExecResult(
                items=[
                    SimpleNamespace(
                        run_id="r1",
                        issue_key="MAB-1",
                        status="queued",
                        pr_url=None,
                        created_at=datetime.now(timezone.utc),
                    )
                ]
            ),
            _ExecResult(scalar_one_or_none=SimpleNamespace(pr_url="https://github/pr/1")),
        ]

        runs_result = dispatcher.dispatch_simple_discord_command(
            session=session,
            tenant=tenant,
            tenant_id="route25",
            payload=payload,
            command_name="runs",
            arguments=["5"],
            jira_browse_base_url=None,
            scope=scope,
        )
        self.assertIsNotNone(runs_result)
        assert runs_result is not None
        self.assertEqual(runs_result.data["runs"][0]["run_id"], "r1")

        with self.assertRaises(HTTPException):
            dispatcher.dispatch_simple_discord_command(
                session=session,
                tenant=tenant,
                tenant_id="route25",
                payload=payload,
                command_name="runs",
                arguments=["bad"],
                jira_browse_base_url=None,
                scope=scope,
            )

        with patch.object(dispatcher, "build_jira_issue_url", return_value="https://jira/MAB-1"):
            link_result = dispatcher.dispatch_simple_discord_command(
                session=session,
                tenant=tenant,
                tenant_id="route25",
                payload=payload,
                command_name="link",
                arguments=["mab-1"],
                jira_browse_base_url="https://jira",
                scope=scope,
            )
        self.assertIsNotNone(link_result)
        assert link_result is not None
        self.assertEqual(link_result.data["jira_url"], "https://jira/MAB-1")

        with self.assertRaises(HTTPException):
            dispatcher.dispatch_simple_discord_command(
                session=session,
                tenant=tenant,
                tenant_id="route25",
                payload=payload,
                command_name="link",
                arguments=[],
                jira_browse_base_url="https://jira",
                scope=scope,
            )

        with patch.object(dispatcher, "_create_allowlist_request", return_value=("req-1", "Request submitted.")):
            request_result = dispatcher.dispatch_simple_discord_command(
                session=session,
                tenant=tenant,
                tenant_id="route25",
                payload=payload,
                command_name="request",
                arguments=["run_controls", "need", "this"],
                jira_browse_base_url=None,
                scope=scope,
            )
        self.assertIsNotNone(request_result)
        assert request_result is not None
        self.assertTrue(request_result.data["requested"])

        with self.assertRaises(HTTPException):
            dispatcher.dispatch_simple_discord_command(
                session=session,
                tenant=tenant,
                tenant_id="route25",
                payload=payload,
                command_name="request",
                arguments=[],
                jira_browse_base_url=None,
                scope=scope,
            )

        with self.assertRaises(HTTPException):
            dispatcher.dispatch_simple_discord_command(
                session=session,
                tenant=tenant,
                tenant_id="route25",
                payload=payload,
                command_name="request",
                arguments=["bad-permission"],
                jira_browse_base_url=None,
                scope=scope,
            )

        none_result = dispatcher.dispatch_simple_discord_command(
            session=session,
            tenant=tenant,
            tenant_id="route25",
            payload=payload,
            command_name="unknown",
            arguments=[],
            jira_browse_base_url=None,
            scope=scope,
        )
        self.assertIsNone(none_result)


if __name__ == "__main__":
    unittest.main()
