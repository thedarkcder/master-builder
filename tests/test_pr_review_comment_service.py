from __future__ import annotations

from types import SimpleNamespace

from orchestrator.api.webhooks.pr_review_comment_service import (
    format_sticky_review_comment,
    publish_inline_review_batch,
    upsert_sticky_remediation_comment,
    upsert_sticky_review_comment,
)
from orchestrator.core.pr_review_findings import PrReviewFindingsResult, ReviewFinding
from orchestrator.core.reviewer import ReviewerSignal
from orchestrator.core.pr_ready import PrReadinessResult


def _signal(*, ready: bool = False) -> ReviewerSignal:
    return ReviewerSignal(
        ready=ready,
        state="ready" if ready else "blocked",
        message="summary",
        readiness=PrReadinessResult(
            ready=ready,
            state="ready" if ready else "blocked",
            reason="r",
            missing_workflows=(),
            pending_workflows=(),
            failing_workflows=(),
        ),
    )


def test_upsert_sticky_review_comment_creates_and_updates() -> None:
    github_client = SimpleNamespace(
        list_pull_request_issue_comments=lambda **_kwargs: [],
        create_pull_request_issue_comment=lambda **_kwargs: SimpleNamespace(comment_id=101),
        update_issue_comment=lambda **_kwargs: SimpleNamespace(comment_id=101),
    )
    created = upsert_sticky_review_comment(
        github_client=github_client,
        repo_full_name="org/repo",
        pr_number=10,
        tenant_id="t1",
        project_id="p1",
        signal=_signal(ready=False),
        findings_result=PrReviewFindingsResult(state="blocked", summary="needs fixes", findings=()),
        event="pull_request",
        action="opened",
    )
    assert created.action == "created"
    assert created.comment_id == 101

    marker_body = "<!-- codex:pr-review:t1:p1:org/repo:10 -->"
    github_client = SimpleNamespace(
        list_pull_request_issue_comments=lambda **_kwargs: [SimpleNamespace(comment_id=101, body=marker_body)],
        create_pull_request_issue_comment=lambda **_kwargs: SimpleNamespace(comment_id=999),
        update_issue_comment=lambda **_kwargs: SimpleNamespace(comment_id=101),
    )
    updated = upsert_sticky_review_comment(
        github_client=github_client,
        repo_full_name="org/repo",
        pr_number=10,
        tenant_id="t1",
        project_id="p1",
        signal=_signal(ready=True),
        findings_result=PrReviewFindingsResult(state="ready", summary="all clear", findings=()),
        event="pull_request",
        action="synchronize",
    )
    assert updated.action == "updated"
    assert updated.comment_id == 101


def test_format_sticky_review_comment_includes_manual_fix_quick_action() -> None:
    body = format_sticky_review_comment(
        signal=_signal(ready=False),
        findings_result=PrReviewFindingsResult(state="blocked", summary="needs fixes", findings=()),
        repo_full_name="org/repo",
        pr_number=10,
        event="pull_request_review",
        action="submitted",
        marker="<!-- marker -->",
    )
    assert "@mb fix <comment-url>" in body
    assert "/mb fix <comment-url>" in body
    assert "https://github.com/org/repo/pull/10#issuecomment-new" in body


def test_publish_inline_review_batch_filters_to_valid_locations() -> None:
    captured: dict[str, object] = {}

    def _submit(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(review_id=555)

    github_client = SimpleNamespace(submit_pull_request_review=_submit)
    result = publish_inline_review_batch(
        github_client=github_client,
        repo_full_name="org/repo",
        pr_number=10,
        head_sha="abc123",
        findings=(
            ReviewFinding(severity="high", message="bad", path="src/a.py", line=11),
            ReviewFinding(severity="low", message="no line", path="src/a.py", line=None),
            ReviewFinding(severity="low", message="not changed", path="src/b.py", line=4),
        ),
        changed_paths={"src/a.py"},
    )
    assert result.submitted is True
    assert result.review_id == 555
    assert result.inline_count == 1
    assert captured["commit_id"] == "abc123"


def test_upsert_sticky_remediation_comment_creates_and_updates() -> None:
    github_client = SimpleNamespace(
        list_pull_request_issue_comments=lambda **_kwargs: [],
        create_pull_request_issue_comment=lambda **_kwargs: SimpleNamespace(comment_id=202),
        update_issue_comment=lambda **_kwargs: SimpleNamespace(comment_id=202),
    )
    created = upsert_sticky_remediation_comment(
        github_client=github_client,
        repo_full_name="org/repo",
        pr_number=10,
        tenant_id="t1",
        project_id="p1",
        issue_key="GP-10",
        issue_url="https://jira.example.com/browse/GP-10",
        issue_created=True,
        enqueued=True,
        reason=None,
        run_id="run-10",
        head_sha="abc123",
        event="pull_request_review_comment",
        action="created",
    )
    assert created.action == "created"
    assert created.comment_id == 202

    marker_body = "<!-- codex:pr-remediation:t1:p1:org/repo:10 -->"
    github_client = SimpleNamespace(
        list_pull_request_issue_comments=lambda **_kwargs: [SimpleNamespace(comment_id=202, body=marker_body)],
        create_pull_request_issue_comment=lambda **_kwargs: SimpleNamespace(comment_id=999),
        update_issue_comment=lambda **_kwargs: SimpleNamespace(comment_id=202),
    )
    updated = upsert_sticky_remediation_comment(
        github_client=github_client,
        repo_full_name="org/repo",
        pr_number=10,
        tenant_id="t1",
        project_id="p1",
        issue_key="GP-10",
        issue_url="https://jira.example.com/browse/GP-10",
        issue_created=False,
        enqueued=False,
        reason="run_already_active",
        run_id="run-10",
        head_sha="abc123",
        event="check_run",
        action="completed",
    )
    assert updated.action == "updated"
    assert updated.comment_id == 202
