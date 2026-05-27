from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json

from sqlalchemy.orm import Session

from orchestrator.api.webhooks.pr_review_publication_state import (
    PR_REVIEW_PUBLICATION_KIND_INLINE,
    PR_REVIEW_PUBLICATION_KIND_STICKY,
    acquire_review_publication,
    mark_review_publication_failed,
    mark_review_publication_published,
)
from orchestrator.core.review.pr_review_findings import (
    PrReviewFindingsResult,
    ReviewFinding,
    pr_review_findings_clear,
)
from orchestrator.core.review.reviewer import ReviewerSignal
from orchestrator.tools.github_app import (
    GitHubAppClient,
    PullRequestInlineCommentDraft,
    PullRequestReviewSubmissionResult,
)

_INLINE_REVIEW_MARKER_PREFIX = "<!-- codex:inline-review:"
_INLINE_REVIEW_MARKER_SUFFIX = " -->"


@dataclass(frozen=True)
class StickyReviewCommentResult:
    action: str
    comment_id: int | None


@dataclass(frozen=True)
class InlineReviewPublishResult:
    submitted: bool
    review_id: int | None
    inline_count: int


def build_review_marker(
    *,
    tenant_id: str,
    project_id: str,
    repo_full_name: str,
    pr_number: int,
) -> str:
    return f"<!-- codex:pr-review:{tenant_id}:{project_id}:{repo_full_name}:{pr_number} -->"


def build_manual_fix_followup_marker(
    *,
    tenant_id: str,
    project_id: str,
    repo_full_name: str,
    pr_number: int,
    triggering_comment_id: int,
) -> str:
    return (
        "<!-- codex:pr-manual-fix:"
        f"{tenant_id}:{project_id}:{repo_full_name}:{pr_number}:{triggering_comment_id}"
        " -->"
    )


def build_manual_fix_issue_comment_reply_marker(
    *,
    tenant_id: str,
    project_id: str,
    repo_full_name: str,
    pr_number: int,
    triggering_comment_id: int,
) -> str:
    return (
        "<!-- codex:pr-manual-fix-issue-comment:"
        f"{tenant_id}:{project_id}:{repo_full_name}:{pr_number}:{triggering_comment_id}"
        " -->"
    )


def format_sticky_review_comment(
    *,
    signal: ReviewerSignal,
    findings_result: PrReviewFindingsResult,
    repo_full_name: str,
    pr_number: int,
    event: str,
    action: str | None,
    marker: str,
) -> str:
    findings_clear = pr_review_findings_clear(findings_result)
    status = "READY" if findings_clear else "BLOCKED"
    state = "ready" if findings_clear else signal.state
    lines = [
        "## Codex PR Review",
        "",
        f"Status: {status}",
        f"State: {state}",
        f"Summary: {findings_result.summary or signal.message}",
        f"Event: {event}/{str(action or 'none').strip() or 'none'}",
    ]
    if findings_result.findings:
        lines.extend(["", "### Findings"])
        for finding in findings_result.findings[:12]:
            location = ""
            if finding.path and finding.line:
                location = f" ({finding.path}:{finding.line})"
            lines.append(f"- [{finding.severity}] {finding.message}{location}")
        compose_url = f"https://github.com/{repo_full_name}/pull/{pr_number}#issuecomment-new"
        lines.extend(
            [
                "",
                "### Queue Fix",
                "Comment on this PR with `@mb <what to change>`.",
                f"[Open comment box]({compose_url})",
            ]
        )
    lines.extend(["", marker])
    return "\n".join(lines).strip()


def upsert_sticky_review_comment(
    *,
    session: Session,
    request_id: str,
    github_client: GitHubAppClient,
    repo_full_name: str,
    pr_number: int,
    tenant_id: str,
    project_id: str,
    head_sha: str,
    signal: ReviewerSignal,
    findings_result: PrReviewFindingsResult,
    event: str,
    action: str | None,
    logger,
) -> StickyReviewCommentResult:
    marker = build_review_marker(
        tenant_id=tenant_id,
        project_id=project_id,
        repo_full_name=repo_full_name,
        pr_number=pr_number,
    )
    comments = github_client.list_pull_request_issue_comments(
        repo_full_name=repo_full_name,
        pr_number=pr_number,
    )
    existing = next((comment for comment in comments if marker in comment.body), None)
    if pr_review_findings_clear(findings_result):
        if existing is None:
            return StickyReviewCommentResult(action="skipped", comment_id=None)
        github_client.delete_issue_comment(
            repo_full_name=repo_full_name,
            comment_id=existing.comment_id,
        )
        return StickyReviewCommentResult(action="deleted", comment_id=existing.comment_id)
    body = format_sticky_review_comment(
        signal=signal,
        findings_result=findings_result,
        repo_full_name=repo_full_name,
        pr_number=pr_number,
        event=event,
        action=action,
        marker=marker,
    )
    signature = hashlib.sha256(body.encode("utf-8")).hexdigest()
    acquisition = acquire_review_publication(
        session,
        tenant_id=tenant_id,
        project_id=project_id,
        repo_full_name=repo_full_name,
        pr_number=pr_number,
        head_sha=str(head_sha or "").strip() or "unknown",
        review_kind=PR_REVIEW_PUBLICATION_KIND_STICKY,
        signature=signature,
        request_id=request_id,
    )
    if not acquisition.acquired:
        logger.info(
            "github_review_publication_skipped request_id=%s tenant_id=%s project_id=%s repo=%s pr_number=%s head_sha=%s kind=%s reason=%s",
            request_id,
            tenant_id,
            project_id,
            repo_full_name,
            pr_number,
            str(head_sha or "").strip() or "unknown",
            PR_REVIEW_PUBLICATION_KIND_STICKY,
            acquisition.reason or "duplicate_signature",
        )
        return StickyReviewCommentResult(action="skipped", comment_id=None)
    try:
        if existing is None:
            created = github_client.create_pull_request_issue_comment(
                repo_full_name=repo_full_name,
                pr_number=pr_number,
                body=body,
            )
            mark_review_publication_published(
                session,
                publication=acquisition.publication,
                review_id=None,
            )
            return StickyReviewCommentResult(action="created", comment_id=created.comment_id)
        updated = github_client.update_issue_comment(
            repo_full_name=repo_full_name,
            comment_id=existing.comment_id,
            body=body,
        )
        mark_review_publication_published(
            session,
            publication=acquisition.publication,
            review_id=None,
        )
        return StickyReviewCommentResult(action="updated", comment_id=updated.comment_id)
    except Exception as exc:
        mark_review_publication_failed(
            session,
            publication=acquisition.publication,
            error=str(exc),
        )
        raise


def format_manual_fix_followup_comment(
    *,
    requested_by: str | None,
    triggering_comment_url: str | None,
    instruction_text: str | None,
    issue_key: str | None,
    issue_url: str | None,
    enqueued: bool,
    run_id: str | None,
    reason: str | None,
    marker: str,
    status_label: str | None = None,
    pr_url: str | None = None,
    change_summary: tuple[str, ...] = (),
) -> str:
    status = str(status_label or "").strip().upper()
    if not status:
        status = "ENQUEUED" if enqueued else "BLOCKED"
    requested_by_text = f"@{requested_by}" if requested_by else "unknown"
    issue_reference = issue_key or "none"
    if issue_key and issue_url:
        issue_reference = f"[{issue_key}]({issue_url})"
    lines = [
        "## Codex Manual Fix",
        "",
        f"Status: {status}",
    ]
    if triggering_comment_url:
        lines.append(f"Command comment: {triggering_comment_url}")
    lines.append(f"Requested by: {requested_by_text}")
    if instruction_text:
        lines.append(f"Instruction: {instruction_text}")
    lines.append(f"Issue: {issue_reference}")
    if run_id:
        lines.append(f"Run ID: {run_id}")
    if pr_url:
        lines.append(f"PR: {pr_url}")
    if reason:
        lines.append(f"Reason: {reason}")
    if status == "SUCCEEDED" and change_summary:
        lines.extend(["", "### What Changed"])
        for item in change_summary[:3]:
            normalized = str(item).strip()
            if normalized:
                lines.append(f"- {normalized}")
    lines.extend(["", marker])
    return "\n".join(lines).strip()


def upsert_manual_fix_review_thread_reply(
    *,
    github_client: GitHubAppClient,
    repo_full_name: str,
    pr_number: int,
    tenant_id: str,
    project_id: str,
    triggering_comment_id: int,
    requested_by: str | None,
    triggering_comment_url: str | None,
    instruction_text: str | None,
    issue_key: str | None,
    issue_url: str | None,
    enqueued: bool,
    run_id: str | None,
    reason: str | None,
    status_label: str | None = None,
    pr_url: str | None = None,
    change_summary: tuple[str, ...] = (),
) -> StickyReviewCommentResult:
    marker = build_manual_fix_followup_marker(
        tenant_id=tenant_id,
        project_id=project_id,
        repo_full_name=repo_full_name,
        pr_number=pr_number,
        triggering_comment_id=triggering_comment_id,
    )
    body = format_manual_fix_followup_comment(
        requested_by=requested_by,
        triggering_comment_url=triggering_comment_url,
        instruction_text=instruction_text,
        issue_key=issue_key,
        issue_url=issue_url,
        enqueued=enqueued,
        run_id=run_id,
        reason=reason,
        marker=marker,
        status_label=status_label,
        pr_url=pr_url,
        change_summary=change_summary,
    )
    comments = github_client.list_pull_request_review_comments(
        repo_full_name=repo_full_name,
        pr_number=pr_number,
    )
    existing = next((comment for comment in comments if marker in comment.body), None)
    if existing is None:
        created = github_client.create_pull_request_review_comment_reply(
            repo_full_name=repo_full_name,
            pr_number=pr_number,
            in_reply_to=triggering_comment_id,
            body=body,
        )
        return StickyReviewCommentResult(action="created", comment_id=created.comment_id)
    updated = github_client.update_pull_request_review_comment(
        repo_full_name=repo_full_name,
        comment_id=existing.comment_id,
        body=body,
    )
    return StickyReviewCommentResult(action="updated", comment_id=updated.comment_id)


def upsert_manual_fix_issue_comment_reply(
    *,
    github_client: GitHubAppClient,
    repo_full_name: str,
    pr_number: int,
    tenant_id: str,
    project_id: str,
    triggering_comment_id: int,
    requested_by: str | None,
    triggering_comment_url: str | None,
    instruction_text: str | None,
    issue_key: str | None,
    issue_url: str | None,
    enqueued: bool,
    run_id: str | None,
    reason: str | None,
    status_label: str | None = None,
    pr_url: str | None = None,
    change_summary: tuple[str, ...] = (),
) -> StickyReviewCommentResult:
    marker = build_manual_fix_issue_comment_reply_marker(
        tenant_id=tenant_id,
        project_id=project_id,
        repo_full_name=repo_full_name,
        pr_number=pr_number,
        triggering_comment_id=triggering_comment_id,
    )
    body = format_manual_fix_followup_comment(
        requested_by=requested_by,
        triggering_comment_url=triggering_comment_url,
        instruction_text=instruction_text,
        issue_key=issue_key,
        issue_url=issue_url,
        enqueued=enqueued,
        run_id=run_id,
        reason=reason,
        marker=marker,
        status_label=status_label,
        pr_url=pr_url,
        change_summary=change_summary,
    )
    comments = github_client.list_pull_request_issue_comments(
        repo_full_name=repo_full_name,
        pr_number=pr_number,
    )
    existing = next((comment for comment in comments if marker in comment.body), None)
    if existing is None:
        created = github_client.create_pull_request_issue_comment(
            repo_full_name=repo_full_name,
            pr_number=pr_number,
            body=body,
        )
        return StickyReviewCommentResult(action="created", comment_id=created.comment_id)
    updated = github_client.update_issue_comment(
        repo_full_name=repo_full_name,
        comment_id=existing.comment_id,
        body=body,
    )
    return StickyReviewCommentResult(action="updated", comment_id=updated.comment_id)


def publish_inline_review_batch(
    *,
    session: Session,
    request_id: str,
    github_client: GitHubAppClient,
    repo_full_name: str,
    pr_number: int,
    head_sha: str,
    tenant_id: str,
    project_id: str,
    findings: tuple[ReviewFinding, ...],
    changed_paths: set[str],
    logger,
) -> InlineReviewPublishResult:
    drafts: list[PullRequestInlineCommentDraft] = []
    seen: set[tuple[str, int, str]] = set()
    for finding in findings:
        path = str(finding.path or "").strip()
        if not path or path not in changed_paths:
            continue
        line = finding.line if isinstance(finding.line, int) and finding.line > 0 else None
        if line is None:
            continue
        message = f"[{finding.severity}] {finding.message}".strip()
        if finding.suggestion:
            message = f"{message}\n\nSuggestion: {finding.suggestion}"
        dedupe_key = (path, line, message)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        drafts.append(PullRequestInlineCommentDraft(path=path, line=line, body=message))

    if not drafts:
        return InlineReviewPublishResult(submitted=False, review_id=None, inline_count=0)

    signature = _build_inline_review_signature(head_sha=head_sha, drafts=drafts)
    acquisition = acquire_review_publication(
        session,
        tenant_id=tenant_id,
        project_id=project_id,
        repo_full_name=repo_full_name,
        pr_number=pr_number,
        head_sha=str(head_sha or "").strip() or "unknown",
        review_kind=PR_REVIEW_PUBLICATION_KIND_INLINE,
        signature=signature,
        request_id=request_id,
    )
    if not acquisition.acquired:
        logger.info(
            "github_review_publication_skipped request_id=%s tenant_id=%s project_id=%s repo=%s pr_number=%s head_sha=%s kind=%s reason=%s",
            request_id,
            tenant_id,
            project_id,
            repo_full_name,
            pr_number,
            str(head_sha or "").strip() or "unknown",
            PR_REVIEW_PUBLICATION_KIND_INLINE,
            acquisition.reason or "duplicate_signature",
        )
        return InlineReviewPublishResult(submitted=False, review_id=None, inline_count=0)
    if _inline_review_signature_exists(
        github_client=github_client,
        repo_full_name=repo_full_name,
        pr_number=pr_number,
        signature=signature,
    ):
        mark_review_publication_published(
            session,
            publication=acquisition.publication,
            review_id=None,
        )
        return InlineReviewPublishResult(submitted=False, review_id=None, inline_count=0)

    marker = _build_inline_review_marker(signature=signature)
    try:
        review_result: PullRequestReviewSubmissionResult = github_client.submit_pull_request_review(
            repo_full_name=repo_full_name,
            pr_number=pr_number,
            commit_id=head_sha,
            body=f"Codex inline review findings.\n\n{marker}",
            comments=drafts,
        )
    except Exception as exc:
        mark_review_publication_failed(
            session,
            publication=acquisition.publication,
            error=str(exc),
        )
        raise
    mark_review_publication_published(
        session,
        publication=acquisition.publication,
        review_id=review_result.review_id,
    )
    return InlineReviewPublishResult(submitted=True, review_id=review_result.review_id, inline_count=len(drafts))


def _build_inline_review_signature(
    *,
    head_sha: str,
    drafts: list[PullRequestInlineCommentDraft],
) -> str:
    normalized_entries = sorted(
        (draft.path, int(draft.line), draft.body.strip())
        for draft in drafts
    )
    payload = {
        "head_sha": str(head_sha or "").strip(),
        "comments": normalized_entries,
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _build_inline_review_marker(*, signature: str) -> str:
    return f"{_INLINE_REVIEW_MARKER_PREFIX}{signature}{_INLINE_REVIEW_MARKER_SUFFIX}"


def _extract_inline_review_signatures(*, review_body: str) -> set[str]:
    body = str(review_body or "")
    signatures: set[str] = set()
    cursor = 0
    while True:
        start = body.find(_INLINE_REVIEW_MARKER_PREFIX, cursor)
        if start == -1:
            break
        value_start = start + len(_INLINE_REVIEW_MARKER_PREFIX)
        end = body.find(_INLINE_REVIEW_MARKER_SUFFIX, value_start)
        if end == -1:
            break
        signature = body[value_start:end].strip()
        if signature:
            signatures.add(signature)
        cursor = end + len(_INLINE_REVIEW_MARKER_SUFFIX)
    return signatures


def _inline_review_signature_exists(
    *,
    github_client: GitHubAppClient,
    repo_full_name: str,
    pr_number: int,
    signature: str,
) -> bool:
    list_reviews = getattr(github_client, "list_pull_request_reviews", None)
    if not callable(list_reviews):
        return False
    try:
        reviews = list_reviews(repo_full_name=repo_full_name, pr_number=pr_number)
    except Exception:  # noqa: BLE001
        return False
    for review in reviews:
        body = getattr(review, "body", None)
        if not isinstance(body, str) or not body.strip():
            continue
        if signature in _extract_inline_review_signatures(review_body=body):
            return True
    return False
