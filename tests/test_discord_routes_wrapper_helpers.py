from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from orchestrator.api.discord.ingress import bug_runtime, executor, gap_runtime, jira_runtime


class DiscordRouteWrapperHelperTests(unittest.TestCase):
    def test_runtime_helper_delegations(self) -> None:
        tenant = SimpleNamespace(tenant_id="route25")
        session = MagicMock()

        with patch.object(gap_runtime, "_tenant_repo_url_impl", return_value="https://example/repo") as impl:
            self.assertEqual(gap_runtime.tenant_repo_url(tenant), "https://example/repo")
            impl.assert_called_once_with(tenant)

        with patch.object(jira_runtime, "fetch_jira_issue_preview_for_tenant", return_value="preview") as impl:
            self.assertEqual(jira_runtime.fetch_jira_issue_preview(session=session, tenant=tenant, issue_key="MAB-1"), "preview")
            impl.assert_called_once()

        with patch.object(gap_runtime, "_extract_acceptance_criteria_from_description_impl", return_value=["a"]) as impl:
            self.assertEqual(gap_runtime.extract_acceptance_criteria_from_description("desc"), ["a"])
            impl.assert_called_once_with("desc")

        with patch.object(gap_runtime, "_gap_confidence_impl", return_value="high") as impl:
            self.assertEqual(
                gap_runtime.gap_confidence(has_acceptance=True, has_successful_run=True, has_pr=False),
                "high",
            )
            impl.assert_called_once()

        with patch.object(gap_runtime, "_run_gap_analysis_impl", return_value=("ok", {})) as impl:
            self.assertEqual(
                gap_runtime.run_gap_analysis(session=session, tenant=tenant, issue_key="MAB-1"),
                ("ok", {}),
            )
            impl.assert_called_once()

        with (
            patch.object(
                bug_runtime,
                "get_settings",
                return_value=SimpleNamespace(discord_bot_token_secret_ref="ref", secrets_encryption_key="key"),
            ),
            patch.object(bug_runtime, "_resolve_discord_channel_name_impl", return_value="triage") as impl,
        ):
            self.assertEqual(bug_runtime.resolve_discord_channel_name(session=session, tenant=tenant, channel_id="c1"), "triage")
            impl.assert_called_once()

        with patch.object(bug_runtime, "_download_discord_attachment_impl", return_value=(b"x", "text/plain")) as impl:
            self.assertEqual(bug_runtime.download_discord_attachment(url="https://example/file"), (b"x", "text/plain"))
            impl.assert_called_once_with(url="https://example/file", bot_token=None)

    def test_ask_history_and_existing_issue_key_helpers(self) -> None:
        tenant = SimpleNamespace(tenant_id="route25")
        session = MagicMock()

        self.assertEqual(
            executor._existing_issue_keys_for_tenant(
                session=session, tenant=tenant, channel_id="c1", issue_keys=set()
            ),
            set(),
        )
        self.assertEqual(
            executor._existing_issue_keys_for_tenant(
                session=session, tenant=tenant, channel_id="c1", issue_keys={" ", ""}
            ),
            set(),
        )

        with (
            patch.object(executor, "_project_filter_jql", return_value='project in ("MAB")'),
            patch.object(
                executor,
                "_search_jira_issues_for_tenant",
                return_value=[SimpleNamespace(key="mab-1"), SimpleNamespace(key="")],
            ) as search_mock,
        ):
            result = executor._existing_issue_keys_for_tenant(
                session=session,
                tenant=tenant,
                channel_id="c1",
                issue_keys={" mab-1 ", "MAB-2", "MAB-2"},
            )
        self.assertEqual(result, {"MAB-1"})
        self.assertIn("key in (\"MAB-1\", \"MAB-2\")", search_mock.call_args.kwargs["jql"])

        with patch.object(executor, "_prune_missing_issue_keys_from_ask_history_impl", return_value=4) as impl:
            self.assertEqual(
                executor._prune_missing_issue_keys_from_ask_history(
                    session=session, tenant=tenant, user_id="u1", channel_id="c1"
                ),
                4,
            )
            self.assertTrue(callable(impl.call_args.kwargs["existing_issue_keys_fn"]))

        with patch.object(executor, "_consume_pending_ask_action_impl", return_value={"request_id": "r1"}) as impl:
            self.assertEqual(
                executor.consume_pending_ask_action(session=session, tenant=tenant, request_id="r1"),
                {"request_id": "r1"},
            )
            impl.assert_called_once()

        with patch.object(executor, "_tenant_ask_history_impl", return_value=[{"a": 1}]) as impl:
            self.assertEqual(executor._tenant_ask_history(tenant), [{"a": 1}])
            impl.assert_called_once_with(tenant=tenant)

        with patch.object(executor, "_recent_ask_history_impl", return_value=[{"q": "x"}]) as impl:
            self.assertEqual(
                executor._recent_ask_history(tenant=tenant, user_id="u1", channel_id="c1", limit=3),
                [{"q": "x"}],
            )
            impl.assert_called_once()


if __name__ == "__main__":
    unittest.main()
