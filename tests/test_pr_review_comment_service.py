from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from orchestrator.api.webhooks.pr_review_publication_state import (
    PR_REVIEW_PUBLICATION_KIND_INLINE,
    acquire_review_publication,
    mark_review_publication_failed,
    mark_review_publication_published,
)
from orchestrator.api.webhooks.pr_review_comment_service import (
    format_sticky_review_comment,
    upsert_manual_fix_issue_comment_reply,
    publish_inline_review_batch,
    upsert_manual_fix_review_thread_reply,
    upsert_sticky_review_comment,
)
from orchestrator.core.pr_review_findings import PrReviewFindingsResult, ReviewFinding
from orchestrator.core.reviewer import ReviewerSignal
from orchestrator.core.pr_ready import PrReadinessResult
from orchestrator.storage.models import Base, PrReviewPublication, Project, Tenant


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


@contextmanager
def _review_session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(
        engine,
        tables=[Tenant.__table__, Project.__table__, PrReviewPublication.__table__],
    )
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    session: Session = session_factory()
    now = datetime.now(timezone.utc)
    session.add(
        Tenant(
            tenant_id="t1",
            name="Tenant 1",
            is_enabled=True,
            jira_config={},
            github_config={},
            repos_config={},
            policy_config={},
            discord_config={},
            created_at=now,
            updated_at=now,
        )
    )
    session.add(
        Project(
            project_id="p1",
            tenant_id="t1",
            name="Project 1",
            github_repository="org/repo",
            jira_project_key="GP",
            policy_overrides={},
            environment={},
            secret_refs={},
            discord_config={},
            is_archived=False,
            created_at=now,
            updated_at=now,
        )
    )
    session.commit()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_upsert_sticky_review_comment_creates_and_updates() -> None:
    with _review_session() as session:
        github_client = SimpleNamespace(
            list_pull_request_issue_comments=lambda **_kwargs: [],
            create_pull_request_issue_comment=lambda **_kwargs: SimpleNamespace(comment_id=101),
            update_issue_comment=lambda **_kwargs: SimpleNamespace(comment_id=101),
        )
        created = upsert_sticky_review_comment(
            session=session,
            request_id="req-1",
            github_client=github_client,
            repo_full_name="org/repo",
            pr_number=10,
            tenant_id="t1",
            project_id="p1",
            head_sha="sha-1",
            signal=_signal(ready=False),
            findings_result=PrReviewFindingsResult(state="blocked", summary="needs fixes", findings=()),
            event="pull_request",
            action="opened",
            logger=SimpleNamespace(info=lambda *args, **kwargs: None),
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
            session=session,
            request_id="req-2",
            github_client=github_client,
            repo_full_name="org/repo",
            pr_number=10,
            tenant_id="t1",
            project_id="p1",
            head_sha="sha-2",
            signal=_signal(ready=True),
            findings_result=PrReviewFindingsResult(state="ready", summary="all clear", findings=()),
            event="pull_request",
            action="synchronize",
            logger=SimpleNamespace(info=lambda *args, **kwargs: None),
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
    assert "@mb <what to change>" in body
    assert "https://github.com/org/repo/pull/10#issuecomment-new" in body


def test_publish_inline_review_batch_filters_to_valid_locations() -> None:
    captured: dict[str, object] = {}

    def _submit(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(review_id=555)

    with _review_session() as session:
        github_client = SimpleNamespace(
            list_pull_request_reviews=lambda **_kwargs: [],
            submit_pull_request_review=_submit,
        )
        result = publish_inline_review_batch(
            session=session,
            request_id="req-1",
            github_client=github_client,
            repo_full_name="org/repo",
            pr_number=10,
            head_sha="abc123",
            tenant_id="t1",
            project_id="p1",
            findings=(
                ReviewFinding(severity="high", message="bad", path="src/a.py", line=11),
                ReviewFinding(severity="low", message="no line", path="src/a.py", line=None),
                ReviewFinding(severity="low", message="not changed", path="src/b.py", line=4),
            ),
            changed_paths={"src/a.py"},
            logger=SimpleNamespace(info=lambda *args, **kwargs: None),
        )
        assert result.submitted is True
        assert result.review_id == 555
        assert result.inline_count == 1
        assert captured["commit_id"] == "abc123"
        assert "<!-- codex:inline-review:" in str(captured["body"])


def test_publish_inline_review_batch_skips_duplicate_signature_for_same_sha() -> None:
    captured: dict[str, object] = {}

    def _submit(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(review_id=444)

    with _review_session() as session:
        initial_client = SimpleNamespace(
            list_pull_request_reviews=lambda **_kwargs: [],
            submit_pull_request_review=_submit,
        )
        initial_result = publish_inline_review_batch(
            session=session,
            request_id="req-1",
            github_client=initial_client,
            repo_full_name="org/repo",
            pr_number=11,
            head_sha="sha-1",
            tenant_id="t1",
            project_id="p1",
            findings=(ReviewFinding(severity="high", message="bad", path="src/a.py", line=11),),
            changed_paths={"src/a.py"},
            logger=SimpleNamespace(info=lambda *args, **kwargs: None),
        )
        assert initial_result.submitted is True

        duplicate_client = SimpleNamespace(
            list_pull_request_reviews=lambda **_kwargs: [SimpleNamespace(body=str(captured["body"]))],
            submit_pull_request_review=lambda **_kwargs: (_ for _ in ()).throw(
                AssertionError("submit_pull_request_review should not be called for duplicate signature")
            ),
        )
        duplicate_result = publish_inline_review_batch(
            session=session,
            request_id="req-2",
            github_client=duplicate_client,
            repo_full_name="org/repo",
            pr_number=11,
            head_sha="sha-1",
            tenant_id="t1",
            project_id="p1",
            findings=(ReviewFinding(severity="high", message="bad", path="src/a.py", line=11),),
            changed_paths={"src/a.py"},
            logger=SimpleNamespace(info=lambda *args, **kwargs: None),
        )
        assert duplicate_result.submitted is False
        assert duplicate_result.review_id is None
        assert duplicate_result.inline_count == 0


def test_publish_inline_review_batch_reposts_when_findings_change_on_same_sha() -> None:
    first_captured: dict[str, object] = {}
    second_captured: dict[str, object] = {}

    def _first_submit(**kwargs):
        first_captured.update(kwargs)
        return SimpleNamespace(review_id=601)

    with _review_session() as session:
        first_client = SimpleNamespace(
            list_pull_request_reviews=lambda **_kwargs: [],
            submit_pull_request_review=_first_submit,
        )
        first_result = publish_inline_review_batch(
            session=session,
            request_id="req-1",
            github_client=first_client,
            repo_full_name="org/repo",
            pr_number=12,
            head_sha="sha-2",
            tenant_id="t1",
            project_id="p1",
            findings=(ReviewFinding(severity="high", message="bad", path="src/a.py", line=11),),
            changed_paths={"src/a.py"},
            logger=SimpleNamespace(info=lambda *args, **kwargs: None),
        )
        assert first_result.submitted is True

        def _second_submit(**kwargs):
            second_captured.update(kwargs)
            return SimpleNamespace(review_id=602)

        second_client = SimpleNamespace(
            list_pull_request_reviews=lambda **_kwargs: [SimpleNamespace(body=str(first_captured["body"]))],
            submit_pull_request_review=_second_submit,
        )
        second_result = publish_inline_review_batch(
            session=session,
            request_id="req-2",
            github_client=second_client,
            repo_full_name="org/repo",
            pr_number=12,
            head_sha="sha-2",
            tenant_id="t1",
            project_id="p1",
            findings=(ReviewFinding(severity="high", message="worse", path="src/a.py", line=11),),
            changed_paths={"src/a.py"},
            logger=SimpleNamespace(info=lambda *args, **kwargs: None),
        )
        assert second_result.submitted is True
        assert second_result.review_id == 602
        assert "<!-- codex:inline-review:" in str(second_captured["body"])
        assert second_captured["body"] != first_captured["body"]


def test_review_publication_state_blocks_duplicate_and_recovers_after_failure() -> None:
    with _review_session() as session:
        acquired = acquire_review_publication(
            session,
            tenant_id="t1",
            project_id="p1",
            repo_full_name="org/repo",
            pr_number=13,
            head_sha="sha-3",
            review_kind=PR_REVIEW_PUBLICATION_KIND_INLINE,
            signature="sig-1",
            request_id="req-1",
        )
        assert acquired.acquired is True
        assert acquired.publication is not None

        duplicate = acquire_review_publication(
            session,
            tenant_id="t1",
            project_id="p1",
            repo_full_name="org/repo",
            pr_number=13,
            head_sha="sha-3",
            review_kind=PR_REVIEW_PUBLICATION_KIND_INLINE,
            signature="sig-1",
            request_id="req-2",
        )
        assert duplicate.acquired is False
        assert duplicate.reason == "lease_held"

        mark_review_publication_failed(
            session,
            publication=acquired.publication,
            error="publish failed",
        )

        reacquired = acquire_review_publication(
            session,
            tenant_id="t1",
            project_id="p1",
            repo_full_name="org/repo",
            pr_number=13,
            head_sha="sha-3",
            review_kind=PR_REVIEW_PUBLICATION_KIND_INLINE,
            signature="sig-1",
            request_id="req-3",
        )
        assert reacquired.acquired is True
        mark_review_publication_published(
            session,
            publication=reacquired.publication,
            review_id=701,
        )

        published_duplicate = acquire_review_publication(
            session,
            tenant_id="t1",
            project_id="p1",
            repo_full_name="org/repo",
            pr_number=13,
            head_sha="sha-3",
            review_kind=PR_REVIEW_PUBLICATION_KIND_INLINE,
            signature="sig-1",
            request_id="req-4",
        )
        assert published_duplicate.acquired is False
        assert published_duplicate.reason == "duplicate_signature"


def test_review_publication_state_persists_large_review_id() -> None:
    with _review_session() as session:
        acquired = acquire_review_publication(
            session,
            tenant_id="t1",
            project_id="p1",
            repo_full_name="org/repo",
            pr_number=13,
            head_sha="sha-3",
            review_kind=PR_REVIEW_PUBLICATION_KIND_INLINE,
            signature="sig-large",
            request_id="req-large",
        )
        assert acquired.acquired is True
        assert acquired.publication is not None

        large_review_id = 4_294_967_299
        mark_review_publication_published(
            session,
            publication=acquired.publication,
            review_id=large_review_id,
        )
        session.commit()

        persisted = session.get(PrReviewPublication, acquired.publication.publication_id)
        assert persisted is not None
        assert persisted.review_id == large_review_id


def test_upsert_manual_fix_review_thread_reply_creates_and_updates() -> None:
    github_client = SimpleNamespace(
        list_pull_request_review_comments=lambda **_kwargs: [],
        create_pull_request_review_comment_reply=lambda **_kwargs: SimpleNamespace(comment_id=303),
        update_pull_request_review_comment=lambda **_kwargs: SimpleNamespace(comment_id=303),
    )
    created = upsert_manual_fix_review_thread_reply(
        github_client=github_client,
        repo_full_name="org/repo",
        pr_number=10,
        tenant_id="t1",
        project_id="p1",
        triggering_comment_id=9001,
        requested_by="alice",
        triggering_comment_url="https://github.com/org/repo/pull/10#discussion_r9001",
        instruction_text="rename the method and add tests",
        issue_key="GP-10",
        issue_url="https://jira.example.com/browse/GP-10",
        enqueued=True,
        run_id="run-10",
        reason=None,
    )
    assert created.action == "created"
    assert created.comment_id == 303

    marker_body = "<!-- codex:pr-manual-fix:t1:p1:org/repo:10:9001 -->"
    github_client = SimpleNamespace(
        list_pull_request_review_comments=lambda **_kwargs: [SimpleNamespace(comment_id=303, body=marker_body)],
        create_pull_request_review_comment_reply=lambda **_kwargs: SimpleNamespace(comment_id=999),
        update_pull_request_review_comment=lambda **_kwargs: SimpleNamespace(comment_id=303),
    )
    updated = upsert_manual_fix_review_thread_reply(
        github_client=github_client,
        repo_full_name="org/repo",
        pr_number=10,
        tenant_id="t1",
        project_id="p1",
        triggering_comment_id=9001,
        requested_by="alice",
        triggering_comment_url="https://github.com/org/repo/pull/10#discussion_r9001",
        instruction_text="rename the method and add tests",
        issue_key="GP-10",
        issue_url="https://jira.example.com/browse/GP-10",
        enqueued=False,
        run_id=None,
        reason="manual_fix_comment_not_found",
    )
    assert updated.action == "updated"
    assert updated.comment_id == 303


def test_upsert_manual_fix_review_thread_reply_renders_terminal_success_summary() -> None:
    captured: dict[str, object] = {}
    github_client = SimpleNamespace(
        list_pull_request_review_comments=lambda **_kwargs: [],
        create_pull_request_review_comment_reply=lambda **kwargs: captured.update(kwargs) or SimpleNamespace(comment_id=404),
        update_pull_request_review_comment=lambda **_kwargs: SimpleNamespace(comment_id=404),
    )
    created = upsert_manual_fix_review_thread_reply(
        github_client=github_client,
        repo_full_name="org/repo",
        pr_number=10,
        tenant_id="t1",
        project_id="p1",
        triggering_comment_id=9001,
        requested_by="alice",
        triggering_comment_url="https://github.com/org/repo/pull/10#discussion_r9001",
        instruction_text="rename the method and add tests",
        issue_key="GP-10",
        issue_url="https://jira.example.com/browse/GP-10",
        enqueued=True,
        run_id="run-10",
        reason=None,
        status_label="SUCCEEDED",
        pr_url="https://github.com/org/repo/pull/10",
        change_summary=("Moved profile sync off the auth path.", "Added targeted auth tests."),
    )
    assert created.action == "created"
    body = str(captured["body"])
    assert "Status: SUCCEEDED" in body
    assert "PR: https://github.com/org/repo/pull/10" in body
    assert "### What Changed" in body
    assert "- Moved profile sync off the auth path." in body


def test_upsert_manual_fix_issue_comment_reply_creates_and_updates() -> None:
    github_client = SimpleNamespace(
        list_pull_request_issue_comments=lambda **_kwargs: [],
        create_pull_request_issue_comment=lambda **_kwargs: SimpleNamespace(comment_id=505),
        update_issue_comment=lambda **_kwargs: SimpleNamespace(comment_id=505),
    )
    created = upsert_manual_fix_issue_comment_reply(
        github_client=github_client,
        repo_full_name="org/repo",
        pr_number=10,
        tenant_id="t1",
        project_id="p1",
        triggering_comment_id=777,
        requested_by="alice",
        triggering_comment_url="https://github.com/org/repo/pull/10#issuecomment-777",
        instruction_text="fix the issue",
        issue_key="GP-10",
        issue_url="https://jira.example.com/browse/GP-10",
        enqueued=True,
        run_id="run-10",
        reason=None,
        status_label="SUCCEEDED",
        pr_url="https://github.com/org/repo/pull/10",
        change_summary=("Applied the requested fix.",),
    )
    assert created.action == "created"
    assert created.comment_id == 505

    marker_body = "<!-- codex:pr-manual-fix-issue-comment:t1:p1:org/repo:10:777 -->"
    github_client = SimpleNamespace(
        list_pull_request_issue_comments=lambda **_kwargs: [SimpleNamespace(comment_id=505, body=marker_body)],
        create_pull_request_issue_comment=lambda **_kwargs: SimpleNamespace(comment_id=999),
        update_issue_comment=lambda **_kwargs: SimpleNamespace(comment_id=505),
    )
    updated = upsert_manual_fix_issue_comment_reply(
        github_client=github_client,
        repo_full_name="org/repo",
        pr_number=10,
        tenant_id="t1",
        project_id="p1",
        triggering_comment_id=777,
        requested_by="alice",
        triggering_comment_url="https://github.com/org/repo/pull/10#issuecomment-777",
        instruction_text="fix the issue",
        issue_key="GP-10",
        issue_url="https://jira.example.com/browse/GP-10",
        enqueued=True,
        run_id="run-10",
        reason=None,
    )
    assert updated.action == "updated"
    assert updated.comment_id == 505
