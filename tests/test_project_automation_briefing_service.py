from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from orchestrator.core.project_automation_briefing_service import _collect_github_facts
from orchestrator.tools.github_app import (
    PullRequestIssueComment,
    PullRequestReview,
    PullRequestReviewComment,
    PullRequestSummary,
)


class ProjectAutomationBriefingServiceTests(unittest.TestCase):
    def test_collect_github_facts_includes_merged_pr_and_review_activity_in_window(self) -> None:
        client = SimpleNamespace(
            list_pull_requests=lambda **_: [
                PullRequestSummary(
                    number=101,
                    title="Ship release",
                    state="closed",
                    html_url="https://github.com/example/repo/pull/101",
                    head_ref="release/ship",
                    base_ref="main",
                    created_at="2026-03-27T08:00:00Z",
                    updated_at="2026-03-28T10:15:00Z",
                    closed_at="2026-03-28T10:15:00Z",
                    merged_at="2026-03-28T10:14:00Z",
                ),
                PullRequestSummary(
                    number=102,
                    title="Old cleanup",
                    state="open",
                    html_url="https://github.com/example/repo/pull/102",
                    head_ref="chore/cleanup",
                    base_ref="main",
                    created_at="2026-03-20T08:00:00Z",
                    updated_at="2026-03-20T09:00:00Z",
                    closed_at=None,
                    merged_at=None,
                ),
            ],
            list_pull_request_reviews=lambda **kwargs: [
                PullRequestReview(
                    review_id=201,
                    state="APPROVED",
                    body="Looks good",
                    submitted_at="2026-03-28T09:45:00Z" if kwargs["pr_number"] == 101 else "2026-03-20T09:30:00Z",
                    user_login="reviewer-1",
                )
            ],
            list_pull_request_review_comments=lambda **kwargs: [
                PullRequestReviewComment(
                    comment_id=301,
                    body="Rename this",
                    path="src/app.py",
                    line=42,
                    state="commented",
                    created_at="2026-03-28T09:50:00Z" if kwargs["pr_number"] == 101 else "2026-03-20T09:45:00Z",
                    user_login="reviewer-2",
                )
            ],
            list_pull_request_issue_comments=lambda **kwargs: [
                PullRequestIssueComment(
                    comment_id=401,
                    body="Merged after validation",
                    created_at="2026-03-28T10:00:00Z" if kwargs["pr_number"] == 101 else "2026-03-20T10:00:00Z",
                    user_login="maintainer",
                )
            ],
        )

        with patch(
            "orchestrator.core.project_automation_briefing_service.github_client_from_tenant_config",
            return_value=client,
        ):
            facts = _collect_github_facts(
                session=SimpleNamespace(),
                settings=SimpleNamespace(secrets_encryption_key=""),
                tenant=SimpleNamespace(tenant_id="tenant-1", github_config={}),
                project=SimpleNamespace(project_id="project-1", github_repository="example/repo"),
                window_start_at=datetime(2026, 3, 28, 9, 0, tzinfo=UTC),
                window_end_at=datetime(2026, 3, 28, 11, 0, tzinfo=UTC),
            )

        self.assertEqual(facts["repo"], "example/repo")
        self.assertEqual(facts["pull_request_count"], 1)
        self.assertEqual(facts["prs_in_window"][0]["number"], 101)
        self.assertEqual(facts["prs_in_window"][0]["merged_at"], "2026-03-28T10:14:00+00:00")
        self.assertEqual(facts["prs_in_window"][0]["review_count"], 1)
        self.assertEqual(facts["prs_in_window"][0]["review_comment_count"], 1)
        self.assertEqual(facts["prs_in_window"][0]["issue_comment_count"], 1)
        self.assertNotIn("body", facts["prs_in_window"][0]["reviews"][0])
        self.assertNotIn("body", facts["prs_in_window"][0]["review_comments"][0])
        self.assertNotIn("body", facts["prs_in_window"][0]["issue_comments"][0])

    def test_collect_github_facts_excludes_prs_with_only_stale_activity(self) -> None:
        client = SimpleNamespace(
            list_pull_requests=lambda **_: [
                PullRequestSummary(
                    number=103,
                    title="Quiet branch",
                    state="open",
                    html_url="https://github.com/example/repo/pull/103",
                    head_ref="feature/quiet",
                    base_ref="main",
                    created_at="2026-03-20T08:00:00Z",
                    updated_at="2026-03-20T09:00:00Z",
                    closed_at=None,
                    merged_at=None,
                )
            ],
            list_pull_request_reviews=lambda **_: [
                PullRequestReview(
                    review_id=202,
                    state="COMMENTED",
                    body="Old note",
                    submitted_at="2026-03-20T09:10:00Z",
                    user_login="reviewer-1",
                )
            ],
            list_pull_request_review_comments=lambda **_: [
                PullRequestReviewComment(
                    comment_id=302,
                    body="Old inline note",
                    path="src/app.py",
                    line=12,
                    state="commented",
                    created_at="2026-03-20T09:15:00Z",
                    user_login="reviewer-2",
                )
            ],
            list_pull_request_issue_comments=lambda **_: [
                PullRequestIssueComment(
                    comment_id=402,
                    body="Old thread",
                    created_at="2026-03-20T09:20:00Z",
                    user_login="maintainer",
                )
            ],
        )

        with patch(
            "orchestrator.core.project_automation_briefing_service.github_client_from_tenant_config",
            return_value=client,
        ):
            facts = _collect_github_facts(
                session=SimpleNamespace(),
                settings=SimpleNamespace(secrets_encryption_key=""),
                tenant=SimpleNamespace(tenant_id="tenant-1", github_config={}),
                project=SimpleNamespace(project_id="project-1", github_repository="example/repo"),
                window_start_at=datetime(2026, 3, 28, 9, 0, tzinfo=UTC),
                window_end_at=datetime(2026, 3, 28, 11, 0, tzinfo=UTC),
            )

        self.assertEqual(facts["pull_request_count"], 0)
        self.assertEqual(facts["prs_in_window"], [])
