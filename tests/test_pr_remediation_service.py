from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from orchestrator.api.webhooks.pr_remediation_issue_service import (
    build_pr_remediation_bug_description,
    find_existing_issue_key_for_pr_head,
)
from orchestrator.api.webhooks.pr_remediation_service import (
    count_pr_remediation_attempts,
    enqueue_pr_remediation_if_needed,
)
from orchestrator.core.review.pr_review_findings import PrReviewFindingsResult
from orchestrator.core.runs.service import EnqueueRunResult
from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot


class PrRemediationServiceTests(unittest.TestCase):
    @staticmethod
    def _trigger_context_from_bootstrap_plan(bootstrap_plan: object) -> dict[str, object]:
        snapshot = ExecutionSnapshot.load(bootstrap_plan)
        assert snapshot is not None
        return dict(snapshot.trigger_context())

    def _base_context(self) -> tuple[MagicMock, SimpleNamespace, SimpleNamespace, MagicMock, dict, SimpleNamespace]:
        session = MagicMock()
        session.commit = MagicMock()
        session.refresh = MagicMock()
        tenant = SimpleNamespace(
            tenant_id="route25",
            jira_config={"connection_id": "conn-1"},
        )
        project = SimpleNamespace(
            project_id="route25-default",
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
        github_client.list_pull_request_files.return_value = []
        payload = {
            "pull_request": {
                "number": 11,
                "head": {"sha": "abc123def456", "ref": "feature/no-key"},
                "base": {"ref": "staging"},
                "title": "Fix auth edge case",
                "body": "Improve auth flow",
                "html_url": "https://github.com/org/repo/pull/11",
            },
            "comment": {
                "id": 777,
                "body": "@mb fix this",
                "html_url": "https://github.com/org/repo/pull/11#discussion_r777",
                "user": {"login": "owner-a"},
                "path": "GirlPower/App/AuthSystem.swift",
                "line": 12,
            },
        }
        settings = SimpleNamespace(secrets_encryption_key="k")
        return session, tenant, project, github_client, payload, settings

    def test_find_existing_issue_key_uses_canonical_trigger_context(self) -> None:
        session = MagicMock()
        matching_plan = ExecutionSnapshot.empty(
            trigger_context={
                "source": "github_pr_review_feedback",
                "pr_number": 11,
                "head_sha": "abc123def456",
            }
        ).dump()
        runs = [
            SimpleNamespace(issue_key="GP-201", plan={"trigger_context": {"source": "legacy"}}, created_at=1),
            SimpleNamespace(issue_key="GP-202", plan=matching_plan, created_at=2),
        ]
        session.execute.return_value.scalars.return_value = runs

        issue_key = find_existing_issue_key_for_pr_head(
            session=session,
            tenant_id="route25",
            project_id="route25-default",
            pr_number=11,
            head_sha="abc123def456",
        )

        self.assertEqual(issue_key, "GP-202")

    def test_find_existing_issue_key_skips_malformed_snapshot_with_warning(self) -> None:
        session = MagicMock()
        matching_plan = ExecutionSnapshot.empty(
            trigger_context={
                "source": "github_pr_review_feedback",
                "pr_number": 11,
                "head_sha": "abc123def456",
            }
        ).dump()
        runs = [
            SimpleNamespace(run_id="run-malformed", issue_key="GP-201", plan={"trigger_context": {"source": "legacy"}}, created_at=1),
            SimpleNamespace(run_id="run-good", issue_key="GP-202", plan=matching_plan, created_at=2),
        ]
        session.execute.return_value.scalars.return_value = runs

        with patch("orchestrator.api.webhooks.pr_remediation_issue_service.logger.warning") as warning_mock:
            issue_key = find_existing_issue_key_for_pr_head(
                session=session,
                tenant_id="route25",
                project_id="route25-default",
                pr_number=11,
                head_sha="abc123def456",
            )

        self.assertEqual(issue_key, "GP-202")
        warning_mock.assert_called_once()

    def test_count_pr_remediation_attempts_uses_canonical_trigger_context(self) -> None:
        session = MagicMock()
        matching_plan = ExecutionSnapshot.empty(
            trigger_context={
                "source": "github_pr_review_feedback",
                "pr_number": 11,
                "head_sha": "abc123def456",
            }
        ).dump()
        runs = [
            SimpleNamespace(plan={"trigger_context": {"source": "legacy"}}),
            SimpleNamespace(plan=matching_plan),
            SimpleNamespace(
                plan=ExecutionSnapshot.empty(
                    trigger_context={
                        "source": "github_pr_review_feedback",
                        "pr_number": 11,
                        "head_sha": "different",
                    }
                ).dump()
            ),
        ]
        session.execute.return_value.scalars.return_value = runs

        total = count_pr_remediation_attempts(
            session=session,
            tenant_id="route25",
            project_id="route25-default",
            issue_key="GP-202",
            pr_number=11,
            head_sha="abc123def456",
        )

        self.assertEqual(total, 1)

    def test_count_pr_remediation_attempts_skips_malformed_snapshot_with_warning(self) -> None:
        session = MagicMock()
        matching_plan = ExecutionSnapshot.empty(
            trigger_context={
                "source": "github_pr_review_feedback",
                "pr_number": 11,
                "head_sha": "abc123def456",
            }
        ).dump()
        runs = [
            SimpleNamespace(run_id="run-malformed", plan={"trigger_context": {"source": "legacy"}}),
            SimpleNamespace(run_id="run-good", plan=matching_plan),
        ]
        session.execute.return_value.scalars.return_value = runs

        with patch("orchestrator.api.webhooks.pr_remediation_service.logger.warning") as warning_mock:
            total = count_pr_remediation_attempts(
                session=session,
                tenant_id="route25",
                project_id="route25-default",
                issue_key="GP-202",
                pr_number=11,
                head_sha="abc123def456",
            )

        self.assertEqual(total, 1)
        warning_mock.assert_called_once()

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
                "orchestrator.api.webhooks.pr_remediation_service.evaluate_pr_review_findings",
                return_value=PrReviewFindingsResult(
                    state="blocked",
                    summary="Fix the failing checks.",
                    findings=(SimpleNamespace(severity="high", message="Fix the failing checks."),),
                ),
            ),
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
        create_bug_mock.assert_not_called()
        enqueue_run_mock.assert_called_once()
        self.assertEqual(enqueue_run_mock.call_args.kwargs["issue_key"], "GP-122")
        bootstrap = enqueue_run_mock.call_args.kwargs["bootstrap"]
        self.assertEqual(bootstrap.branch, "feature/no-key")
        self.assertEqual(bootstrap.pr_url, "https://github.com/org/repo/pull/11")

    def test_existing_issue_key_from_automatic_trigger_is_not_reenqueued(self) -> None:
        session, tenant, project, github_client, payload, settings = self._base_context()
        existing_run = SimpleNamespace(run_id="run-existing", plan={})
        payload = {
            **payload,
            "comment": {
                "id": 777,
                "body": "plain comment",
                "html_url": "https://github.com/org/repo/pull/11#discussion_r777",
                "user": {"login": "owner-a"},
            },
            "check_run": {
                "conclusion": "failure",
            },
        }

        with (
            patch(
                "orchestrator.api.webhooks.pr_remediation_service.evaluate_pr_review_findings",
                return_value=PrReviewFindingsResult(
                    state="blocked",
                    summary="Fix the failing checks.",
                    findings=(SimpleNamespace(severity="high", message="Fix the failing checks."),),
                ),
            ),
            patch(
                "orchestrator.api.webhooks.pr_remediation_policy.find_existing_issue_key_for_pr_head",
                return_value="GP-122",
            ),
            patch(
                "orchestrator.api.webhooks.pr_remediation_service._latest_issue_run",
                return_value=existing_run,
            ) as latest_run_mock,
            patch(
                "orchestrator.api.webhooks.pr_remediation_enqueue.enqueue_run",
            ) as enqueue_run_mock,
        ):
            result = enqueue_pr_remediation_if_needed(
                session=session,
                tenant=tenant,
                project=project,
                github_client=github_client,
                event="check_run",
                action="completed",
                payload=payload,
                pr_number=11,
                repo_full_name="org/repo",
                settings=settings,
            )

        self.assertTrue(result.triggered)
        self.assertFalse(result.enqueued)
        self.assertEqual(result.issue_key, "GP-122")
        self.assertFalse(result.issue_created)
        self.assertEqual(result.reason, "existing_issue_already_tracked")
        self.assertIs(result.run, existing_run)
        latest_run_mock.assert_called_once()
        enqueue_run_mock.assert_not_called()

    def test_check_trigger_with_clear_review_does_not_create_bug(self) -> None:
        session, tenant, project, github_client, payload, settings = self._base_context()
        payload = {
            **payload,
            "comment": {"id": 777, "body": "plain comment"},
            "check_run": {"conclusion": "failure"},
        }

        with (
            patch(
                "orchestrator.api.webhooks.pr_remediation_service.evaluate_pr_review_findings",
                return_value=PrReviewFindingsResult(
                    state="ready",
                    summary="No actionable findings.",
                    findings=(),
                ),
            ),
            patch(
                "orchestrator.api.webhooks.pr_remediation_policy.create_pr_remediation_bug_issue_key",
            ) as create_bug_mock,
            patch("orchestrator.api.webhooks.pr_remediation_enqueue.enqueue_run") as enqueue_run_mock,
        ):
            result = enqueue_pr_remediation_if_needed(
                session=session,
                tenant=tenant,
                project=project,
                github_client=github_client,
                event="check_run",
                action="completed",
                payload=payload,
                pr_number=11,
                repo_full_name="org/repo",
                settings=settings,
            )

        self.assertTrue(result.triggered)
        self.assertFalse(result.enqueued)
        self.assertIsNone(result.issue_key)
        self.assertEqual(result.reason, "review_clear_no_actionable_findings")
        create_bug_mock.assert_not_called()
        enqueue_run_mock.assert_not_called()

    def test_check_trigger_fails_hard_when_findings_cannot_be_evaluated(self) -> None:
        session, tenant, project, github_client, payload, settings = self._base_context()
        payload = {
            **payload,
            "comment": {"id": 777, "body": "plain comment"},
            "check_run": {"conclusion": "failure"},
        }

        with (
            patch(
                "orchestrator.api.webhooks.pr_remediation_service.evaluate_pr_review_findings",
                side_effect=RuntimeError("review service unavailable"),
            ),
            patch(
                "orchestrator.api.webhooks.pr_remediation_policy.create_pr_remediation_bug_issue_key",
            ) as create_bug_mock,
            patch("orchestrator.api.webhooks.pr_remediation_enqueue.enqueue_run") as enqueue_run_mock,
        ):
            result = enqueue_pr_remediation_if_needed(
                session=session,
                tenant=tenant,
                project=project,
                github_client=github_client,
                event="check_run",
                action="completed",
                payload=payload,
                pr_number=11,
                repo_full_name="org/repo",
                settings=settings,
            )

        self.assertTrue(result.triggered)
        self.assertFalse(result.enqueued)
        self.assertIsNone(result.issue_key)
        self.assertEqual(result.reason, "review_findings_evaluation_failed:review service unavailable")
        create_bug_mock.assert_not_called()
        enqueue_run_mock.assert_not_called()

    def test_manual_fix_request_still_enqueues_when_issue_already_exists(self) -> None:
        session, tenant, project, github_client, payload, settings = self._base_context()
        enqueue_result = EnqueueRunResult(
            enqueued=True,
            reason=None,
            run=SimpleNamespace(run_id="run-manual", plan={}),
        )

        with (
            patch(
                "orchestrator.api.webhooks.pr_remediation_policy.find_existing_issue_key_for_pr_head",
                return_value="GP-122",
            ),
            patch(
                "orchestrator.api.webhooks.pr_remediation_service._latest_issue_run",
            ) as latest_run_mock,
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
        latest_run_mock.assert_not_called()
        enqueue_run_mock.assert_called_once()

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

    def test_manual_fix_command_uses_triggering_comment_as_context(self) -> None:
        session, tenant, project, github_client, payload, settings = self._base_context()
        github_client.list_pull_request_issue_comments.return_value = [
            SimpleNamespace(
                comment_id=501,
                body="@mb fix the flaky test",
                created_at="2026-03-15T12:00:00Z",
                user_login="owner-a",
            )
        ]
        payload = {
            **payload,
            "issue": {"number": 11, "pull_request": {"url": "https://api.github.com/repos/org/repo/pulls/11"}},
            "comment": {
                "id": 501,
                "body": "@mb fix the flaky test",
                "html_url": "https://github.com/org/repo/pull/11#issuecomment-501",
                "user": {"login": "owner-a"},
            },
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
        self.assertIsNone(result.reason)
        trigger_context = self._trigger_context_from_bootstrap_plan(
            enqueue_run_mock.call_args.kwargs["bootstrap"].plan
        )
        manual_fix = trigger_context.get("manual_fix_request")
        self.assertIsInstance(manual_fix, dict)
        self.assertEqual(manual_fix.get("instruction_text"), "fix the flaky test")
        requested_comment = manual_fix.get("requested_comment")
        self.assertIsInstance(requested_comment, dict)
        self.assertEqual(requested_comment.get("id"), 501)
        self.assertEqual(requested_comment.get("type"), "issue_comment")

    def test_manual_fix_command_enqueues_with_triggering_comment(self) -> None:
        session, tenant, project, github_client, payload, settings = self._base_context()
        payload = {
            **payload,
            "issue": {"number": 11, "pull_request": {"url": "https://api.github.com/repos/org/repo/pulls/11"}},
            "comment": {
                "id": 550,
                "body": "@mb rename this variable",
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
        trigger_context = self._trigger_context_from_bootstrap_plan(
            enqueue_run_mock.call_args.kwargs["bootstrap"].plan
        )
        manual_fix = trigger_context.get("manual_fix_request")
        self.assertIsInstance(manual_fix, dict)
        self.assertEqual(manual_fix.get("requested_by"), "owner-a")
        self.assertEqual(manual_fix.get("instruction_text"), "rename this variable")
        requested_comment = manual_fix.get("requested_comment")
        self.assertIsInstance(requested_comment, dict)
        self.assertEqual(requested_comment.get("id"), 550)
        self.assertEqual(requested_comment.get("type"), "issue_comment")
        enqueue_run_mock.assert_called_once()

    def test_manual_fix_command_requires_instruction_text(self) -> None:
        session, tenant, project, github_client, payload, settings = self._base_context()
        payload = {
            **payload,
            "issue": {"number": 11, "pull_request": {"url": "https://api.github.com/repos/org/repo/pulls/11"}},
            "comment": {"id": 501, "body": "@mb", "html_url": "https://github.com/org/repo/pull/11#issuecomment-501"},
        }

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
        self.assertFalse(result.enqueued)
        self.assertEqual(result.reason, "manual_fix_missing_instruction")

    def test_manual_fix_review_comment_enqueues_with_only_triggering_comment_and_code_context(self) -> None:
        session, tenant, project, github_client, payload, settings = self._base_context()
        payload = {
            **payload,
            "comment": {
                "id": 777,
                "body": "@mb use a background task here",
                "html_url": "https://github.com/org/repo/pull/11#discussion_r777",
                "user": {"login": "owner-a"},
                "path": "GirlPower/App/AuthSystem.swift",
                "line": 12,
            },
        }
        github_client.get_file_text_at_ref.return_value = "\n".join(
            f"line {number}" for number in range(1, 21)
        )
        enqueue_result = EnqueueRunResult(
            enqueued=True,
            reason=None,
            run=SimpleNamespace(run_id="run-manual-review", plan={}),
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
                event="pull_request_review_comment",
                action="created",
                payload=payload,
                pr_number=11,
                repo_full_name="org/repo",
                settings=settings,
            )

        self.assertTrue(result.triggered)
        self.assertTrue(result.enqueued)
        trigger_context = self._trigger_context_from_bootstrap_plan(
            enqueue_run_mock.call_args.kwargs["bootstrap"].plan
        )
        self.assertNotIn("review_comments", trigger_context)
        self.assertNotIn("issue_comments", trigger_context)
        requested_comment = trigger_context.get("requested_comment")
        self.assertIsInstance(requested_comment, dict)
        self.assertEqual(requested_comment.get("id"), 777)
        self.assertEqual(requested_comment.get("type"), "review_comment")
        code_context = trigger_context.get("code_context")
        self.assertIsInstance(code_context, dict)
        self.assertEqual(code_context.get("path"), "GirlPower/App/AuthSystem.swift")
        self.assertEqual(code_context.get("line"), 12)
        self.assertIn("12: line 12", str(code_context.get("snippet")))
        manual_fix = trigger_context.get("manual_fix_request")
        self.assertIsInstance(manual_fix, dict)
        self.assertIsInstance(manual_fix.get("code_context"), dict)

    def test_untagged_review_comment_does_not_trigger_remediation(self) -> None:
        session, tenant, project, github_client, payload, settings = self._base_context()
        payload = {
            **payload,
            "comment": {
                **payload["comment"],
                "body": "fix this",
            },
        }

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

        self.assertFalse(result.triggered)
        self.assertFalse(result.enqueued)
        self.assertIsNone(result.issue_key)
        github_client.get_pull_request_details.assert_not_called()

    def test_manual_fix_issue_comment_trigger_context_is_comment_only(self) -> None:
        session, tenant, project, github_client, payload, settings = self._base_context()
        payload = {
            **payload,
            "issue": {"number": 11, "pull_request": {"url": "https://api.github.com/repos/org/repo/pulls/11"}},
            "comment": {
                "id": 550,
                "body": "@mb rename this variable",
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
        trigger_context = self._trigger_context_from_bootstrap_plan(
            enqueue_run_mock.call_args.kwargs["bootstrap"].plan
        )
        self.assertNotIn("review_comments", trigger_context)
        self.assertNotIn("issue_comments", trigger_context)
        self.assertIsNone(trigger_context.get("code_context"))
        manual_fix = trigger_context.get("manual_fix_request")
        self.assertIsInstance(manual_fix, dict)
        self.assertIsNone(manual_fix.get("code_context"))

    def test_manual_fix_bug_description_uses_comment_and_code_context_only(self) -> None:
        description = build_pr_remediation_bug_description(
            repo_full_name="org/repo",
            pr_number=11,
            pr_url="https://github.com/org/repo/pull/11",
            head_sha="abc123",
            event="pull_request_review_comment",
            action="created",
            checks=[SimpleNamespace(name="CI", status="completed", conclusion="failure")],
            reviews=[SimpleNamespace(state="CHANGES_REQUESTED", body="please fix")],
            review_comments=[SimpleNamespace(path="A.swift", line=5, body="old comment")],
            issue_comments=[SimpleNamespace(body="old issue comment")],
            manual_fix_request={
                "requested_by": "owner-a",
                "requested_comment": {
                    "url": "https://github.com/org/repo/pull/11#discussion_r777",
                    "body": "@mb use a background task here",
                },
                "instruction_text": "use a background task here",
                "code_context": {
                    "path": "GirlPower/App/AuthSystem.swift",
                    "line": 12,
                    "snippet": "10: a\n11: b\n12: c",
                },
            },
        )

        self.assertIn("Manual request: yes", description)
        self.assertIn("Referenced code: GirlPower/App/AuthSystem.swift:12", description)
        self.assertIn("12: c", description)
        self.assertNotIn("Failing checks:", description)
        self.assertNotIn("Review comments:", description)
        self.assertNotIn("Issue comments:", description)


if __name__ == "__main__":
    unittest.main()
