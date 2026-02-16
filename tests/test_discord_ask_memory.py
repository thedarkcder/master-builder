from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from orchestrator.api.discord.ask import memory as ask_memory


class DiscordAskMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = MagicMock()
        self.tenant = SimpleNamespace(tenant_id="t1")

    def _preview(self, key: str, summary: str, status: str) -> SimpleNamespace:
        return SimpleNamespace(key=key, summary=summary, status=status)

    def test_collect_ask_context_scoped_issue_not_found(self) -> None:
        with (
            patch.object(ask_memory, "_project_filter_jql", return_value='project = "MAB"'),
            patch.object(ask_memory, "_search_jira_issues_for_tenant", return_value=[]),
        ):
            with self.assertRaises(HTTPException) as exc_ctx:
                ask_memory.collect_ask_context(
                    session=self.session,
                    tenant=self.tenant,
                    channel_id="chan-1",
                    question="what is happening",
                    scoped_issue_key="MAB-404",
                )
        self.assertEqual(exc_ctx.exception.status_code, 404)

    def test_collect_ask_context_counts_without_status_filter(self) -> None:
        issues = [
            self._preview("MAB-1", "One", "Blocked"),
            self._preview("MAB-2", "Two", "Blocked"),
            self._preview("MAB-3", "Three", "Done"),
        ]
        with (
            patch.object(ask_memory, "_project_filter_jql", return_value='project = "MAB"'),
            patch.object(ask_memory, "_search_jira_issues_for_tenant", return_value=issues) as search_mock,
        ):
            normalized_issue_key, requested_status, result_issues, counts = ask_memory.collect_ask_context(
                session=self.session,
                tenant=self.tenant,
                channel_id="chan-1",
                question="show me blocked work",
            )

        self.assertIsNone(normalized_issue_key)
        self.assertIsNone(requested_status)
        self.assertEqual(len(result_issues), 3)
        self.assertEqual(counts["Blocked"], 2)
        self.assertNotIn('status = "Blocked"', search_mock.call_args.kwargs["jql"])

    def test_collect_ask_context_default_query_path(self) -> None:
        with (
            patch.object(ask_memory, "_project_filter_jql", return_value='project = "MAB"'),
            patch.object(ask_memory, "_search_jira_issues_for_tenant", return_value=[] ) as search_mock,
        ):
            normalized_issue_key, requested_status, result_issues, counts = ask_memory.collect_ask_context(
                session=self.session,
                tenant=self.tenant,
                channel_id=None,
                question="latest updates",
            )

        self.assertIsNone(normalized_issue_key)
        self.assertIsNone(requested_status)
        self.assertEqual(result_issues, [])
        self.assertEqual(counts, {})
        self.assertIn("ORDER BY updated DESC", search_mock.call_args.kwargs["jql"])

    def test_existing_issue_keys_for_tenant_normalizes_and_limits(self) -> None:
        issues = [self._preview("mab-1", "One", "To Do"), self._preview("MAB-2", "Two", "Done")]
        with (
            patch.object(ask_memory, "_project_filter_jql", return_value='project = "MAB"'),
            patch.object(ask_memory, "_search_jira_issues_for_tenant", return_value=issues) as search_mock,
        ):
            result = ask_memory.existing_issue_keys_for_tenant(
                session=self.session,
                tenant=self.tenant,
                channel_id="chan-1",
                issue_keys={" mab-1 ", "MAB-2", ""},
            )

        self.assertEqual(result, {"MAB-1", "MAB-2"})
        jql = search_mock.call_args.kwargs["jql"]
        self.assertIn("key in", jql)

    def test_prune_and_history_wrapper_paths(self) -> None:
        service = MagicMock()
        service.prune_missing_issue_keys_from_ask_history.return_value = 3
        service.collect_ask_context_with_history_context.return_value = (None, None, [], {}, [])

        with patch.object(ask_memory, "_ask_history_service", service):
            pruned = ask_memory.prune_missing_issue_keys_from_ask_history(
                session=self.session,
                tenant=self.tenant,
                user_id="u1",
                channel_id="c1",
            )
            result = ask_memory.collect_ask_context_with_history_context(
                session=self.session,
                tenant=self.tenant,
                user_id="u1",
                channel_id="c1",
                question="hello",
                scoped_issue_key=None,
            )

        self.assertEqual(pruned, 3)
        self.assertEqual(result, (None, None, [], {}, []))

    def test_store_and_consume_pending_ask_action_wrappers(self) -> None:
        service = MagicMock()
        service.store_pending_ask_action.return_value = {"request_id": "abc"}
        service.consume_pending_ask_action.return_value = {"request_id": "abc"}

        with (
            patch.object(ask_memory, "_ask_history_service", service),
            patch.object(ask_memory, "uuid4") as uuid4_mock,
        ):
            uuid4_mock.return_value.hex = "req123"
            stored = ask_memory.store_pending_ask_action(
                session=self.session,
                tenant=self.tenant,
                user_id="u1",
                channel_id="c1",
                question="q",
                summary="s",
                proposed_command="!run",
            )
            consumed = ask_memory.consume_pending_ask_action(
                session=self.session,
                tenant=self.tenant,
                request_id="req123",
            )

        self.assertEqual(stored["request_id"], "abc")
        self.assertEqual(consumed["request_id"], "abc")


if __name__ == "__main__":
    unittest.main()
