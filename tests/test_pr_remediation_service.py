from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from orchestrator.api.webhooks.pr_remediation_service import enqueue_pr_remediation_if_needed
from orchestrator.core.runs import EnqueueRunResult


class PrRemediationServiceTests(unittest.TestCase):
    def _base_context(self) -> tuple[MagicMock, SimpleNamespace, SimpleNamespace, MagicMock, dict, SimpleNamespace]:
        session = MagicMock()
        session.commit = MagicMock()
        session.refresh = MagicMock()
        tenant = SimpleNamespace(
            tenant_id="example",
            jira_config={"connection_id": "conn-1"},
        )
        project = SimpleNamespace(
            project_id="example-default",
            jira_project_key="GP",
            github_repository="org/repo",
        )
        github_client = MagicMock()
        github_client.get_pull_request_details.return_value = SimpleNamespace(
            head_sha="abc123def456",
            head_ref="feature/no-key",
            base_ref="staging",
            title="Fix auth edge case",
            body="Improve auth flow",
            html_url="https://github.com/org/repo/pull/11",
        )
        github_client.list_check_suites.return_value = []
        github_client.list_pull_request_reviews.return_value = []
        github_client.list_pull_request_review_comments.return_value = []
        github_client.list_pull_request_issue_comments.return_value = []
        payload = {
            "pull_request": {
                "number": 11,
                "head": {"sha": "abc123def456", "ref": "feature/no-key"},
                "base": {"ref": "staging"},
                "title": "Fix auth edge case",
                "body": "Improve auth flow",
                "html_url": "https://github.com/org/repo/pull/11",
            }
        }
        settings = SimpleNamespace(secrets_encryption_key="k")
        return session, tenant, project, github_client, payload, settings

    def test_non_trigger_event_returns_not_triggered(self) -> None:
        session, tenant, project, github_client, payload, settings = self._base_context()

        result = enqueue_pr_remediation_if_needed(
            session=session,
            tenant=tenant,
            project=project,
            github_client=github_client,
            event="pull_request",
            action="synchronize",
            payload=payload,
            pr_number=11,
            repo_full_name="org/repo",
            settings=settings,
        )

        self.assertFalse(result.triggered)
        self.assertFalse(result.enqueued)
        self.assertIsNone(result.issue_key)
        github_client.get_pull_request_details.assert_not_called()

    def test_reuses_existing_issue_key_for_same_pr_head(self) -> None:
        session, tenant, project, github_client, payload, settings = self._base_context()
        queued_run = SimpleNamespace(run_id="run-11", plan={}, branch=None, pr_url=None)
        enqueue_result = EnqueueRunResult(
            enqueued=True,
            reason=None,
            run=queued_run,
        )

        with (
            patch(
                "orchestrator.api.webhooks.pr_remediation_policy.find_existing_issue_key_for_pr_head",
                return_value="GP-122",
            ),
            patch(
                "orchestrator.api.webhooks.pr_remediation_policy.create_pr_remediation_bug_issue_key",
            ) as create_bug_mock,
            patch(
                "orchestrator.api.webhooks.pr_remediation_enqueue.enqueue_run",
                return_value=enqueue_result,
            ) as enqueue_run_mock,
        ):
            result = enqueue_pr_remediation_if_needed(
                session=session,
                tenant=tenant,
                project=project,
                github_client=github_client,
                event="pull_request_review_comment",
                action="created",
                payload=payload,
                pr_number=11,
                repo_full_name="org/repo",
                settings=settings,
            )

        self.assertTrue(result.triggered)
        self.assertTrue(result.enqueued)
        self.assertEqual(result.issue_key, "GP-122")
        self.assertFalse(result.issue_created)
        self.assertEqual(queued_run.branch, "feature/no-key")
        self.assertEqual(queued_run.pr_url, "https://github.com/org/repo/pull/11")
        create_bug_mock.assert_not_called()
        enqueue_run_mock.assert_called_once()
        self.assertEqual(enqueue_run_mock.call_args.kwargs["issue_key"], "GP-122")

    def test_creates_bug_when_issue_key_missing(self) -> None:
        session, tenant, project, github_client, payload, settings = self._base_context()
        enqueue_result = EnqueueRunResult(
            enqueued=True,
            reason=None,
            run=SimpleNamespace(run_id="run-500", plan={}),
        )

        with (
            patch(
                "orchestrator.api.webhooks.pr_remediation_policy.find_existing_issue_key_for_pr_head",
                return_value=None,
            ),
            patch(
                "orchestrator.api.webhooks.pr_remediation_policy.create_pr_remediation_bug_issue_key",
                return_value="GP-500",
            ) as create_bug_mock,
            patch(
                "orchestrator.api.webhooks.pr_remediation_enqueue.enqueue_run",
                return_value=enqueue_result,
            ),
        ):
            result = enqueue_pr_remediation_if_needed(
                session=session,
                tenant=tenant,
                project=project,
                github_client=github_client,
                event="pull_request_review_comment",
                action="created",
                payload=payload,
                pr_number=11,
                repo_full_name="org/repo",
                settings=settings,
            )

        self.assertTrue(result.triggered)
        self.assertTrue(result.enqueued)
        self.assertTrue(result.issue_created)
        self.assertEqual(result.issue_key, "GP-500")
        create_bug_mock.assert_called_once()

    def test_bug_create_failure_blocks_enqueue(self) -> None:
        session, tenant, project, github_client, payload, settings = self._base_context()

        with (
            patch(
                "orchestrator.api.webhooks.pr_remediation_policy.find_existing_issue_key_for_pr_head",
                return_value=None,
            ),
            patch(
                "orchestrator.api.webhooks.pr_remediation_policy.create_pr_remediation_bug_issue_key",
                side_effect=ValueError("jira down"),
            ),
            patch("orchestrator.api.webhooks.pr_remediation_enqueue.enqueue_run") as enqueue_run_mock,
        ):
            result = enqueue_pr_remediation_if_needed(
                session=session,
                tenant=tenant,
                project=project,
                github_client=github_client,
                event="pull_request_review_comment",
                action="created",
                payload=payload,
                pr_number=11,
                repo_full_name="org/repo",
                settings=settings,
            )

        self.assertTrue(result.triggered)
        self.assertFalse(result.enqueued)
        self.assertIsNone(result.issue_key)
        self.assertIn("jira_bug_create_failed", str(result.reason))
        enqueue_run_mock.assert_not_called()

    def test_does_not_mutate_existing_active_run_when_enqueue_conflicts(self) -> None:
        session, tenant, project, github_client, payload, settings = self._base_context()
        active_run = SimpleNamespace(
            run_id="run-active",
            plan={"existing": True},
        )
        enqueue_result = EnqueueRunResult(
            enqueued=False,
            reason="run_already_active",
            run=active_run,
        )

        with (
            patch(
                "orchestrator.api.webhooks.pr_remediation_policy.find_existing_issue_key_for_pr_head",
                return_value="GP-122",
            ),
            patch(
                "orchestrator.api.webhooks.pr_remediation_enqueue.enqueue_run",
                return_value=enqueue_result,
            ),
        ):
            result = enqueue_pr_remediation_if_needed(
                session=session,
                tenant=tenant,
                project=project,
                github_client=github_client,
                event="pull_request_review_comment",
                action="created",
                payload=payload,
                pr_number=11,
                repo_full_name="org/repo",
                settings=settings,
            )

        self.assertTrue(result.triggered)
        self.assertFalse(result.enqueued)
        self.assertEqual(active_run.plan, {"existing": True})
        session.commit.assert_not_called()
        session.refresh.assert_not_called()

    def test_manual_fix_command_without_url_infers_trigger_comment_url(self) -> None:
        session, tenant, project, github_client, payload, settings = self._base_context()
        github_client.list_pull_request_issue_comments.return_value = [
            SimpleNamespace(
                comment_id=501,
                body="@mb fix",
                created_at="2026-03-15T12:00:00Z",
                user_login="owner-a",
            )
        ]
        payload = {
            **payload,
            "issue": {"number": 11, "pull_request": {"url": "https://api.github.com/repos/org/repo/pulls/11"}},
            "comment": {"id": 501, "body": "@mb fix", "html_url": "https://github.com/org/repo/pull/11#issuecomment-501"},
        }
        enqueue_result = EnqueueRunResult(
            enqueued=True,
            reason=None,
            run=SimpleNamespace(run_id="run-manual-inferred", plan={}),
        )

        with (
            patch(
                "orchestrator.api.webhooks.pr_remediation_policy.find_existing_issue_key_for_pr_head",
                return_value="GP-122",
            ),
            patch(
                "orchestrator.api.webhooks.pr_remediation_enqueue.enqueue_run",
                return_value=enqueue_result,
            ),
        ):
            result = enqueue_pr_remediation_if_needed(
                session=session,
                tenant=tenant,
                project=project,
                github_client=github_client,
                event="issue_comment",
                action="created",
                payload=payload,
                pr_number=11,
                repo_full_name="org/repo",
                settings=settings,
            )

        self.assertTrue(result.triggered)
        self.assertTrue(result.enqueued)
        self.assertIsNone(result.reason)

    def test_manual_fix_command_enqueues_with_requested_comment(self) -> None:
        session, tenant, project, github_client, payload, settings = self._base_context()
        github_client.list_pull_request_review_comments.return_value = [
            SimpleNamespace(
                comment_id=222,
                body="Please update variable naming",
                path="app/main.py",
                line=42,
                user_login="reviewer-a",
            )
        ]
        payload = {
            **payload,
            "issue": {"number": 11, "pull_request": {"url": "https://api.github.com/repos/org/repo/pulls/11"}},
            "comment": {
                "id": 550,
                "body": "@mb fix https://github.com/org/repo/pull/11#discussion_r222",
                "html_url": "https://github.com/org/repo/pull/11#issuecomment-550",
                "user": {"login": "owner-a"},
            },
        }
        enqueue_result = EnqueueRunResult(
            enqueued=True,
            reason=None,
            run=SimpleNamespace(run_id="run-manual-11", plan={}),
        )

        with (
            patch(
                "orchestrator.api.webhooks.pr_remediation_policy.find_existing_issue_key_for_pr_head",
                return_value="GP-122",
            ),
            patch(
                "orchestrator.api.webhooks.pr_remediation_enqueue.enqueue_run",
                return_value=enqueue_result,
            ) as enqueue_run_mock,
        ):
            result = enqueue_pr_remediation_if_needed(
                session=session,
                tenant=tenant,
                project=project,
                github_client=github_client,
                event="issue_comment",
                action="created",
                payload=payload,
                pr_number=11,
                repo_full_name="org/repo",
                settings=settings,
            )

        self.assertTrue(result.triggered)
        self.assertTrue(result.enqueued)
        self.assertEqual(result.issue_key, "GP-122")
        manual_fix = enqueue_result.run.plan.get("trigger_context", {}).get("manual_fix_request")
        self.assertIsInstance(manual_fix, dict)
        self.assertEqual(manual_fix.get("requested_by"), "owner-a")
        requested_comment = manual_fix.get("requested_comment")
        self.assertIsInstance(requested_comment, dict)
        self.assertEqual(requested_comment.get("id"), 222)
        self.assertEqual(requested_comment.get("type"), "review_comment")
        enqueue_run_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
