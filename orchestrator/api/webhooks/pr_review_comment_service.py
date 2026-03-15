from __future__ import annotations

from dataclasses import dataclass

from orchestrator.core.pr_review_findings import PrReviewFindingsResult, ReviewFinding
from orchestrator.core.reviewer import ReviewerSignal
from orchestrator.tools.github_app import (
    GitHubAppClient,
    PullRequestInlineCommentDraft,
    PullRequestReviewSubmissionResult,
)


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


def build_remediation_marker(
    *,
    tenant_id: str,
    project_id: str,
    repo_full_name: str,
    pr_number: int,
) -> str:
    return f"<!-- codex:pr-remediation:{tenant_id}:{project_id}:{repo_full_name}:{pr_number} -->"


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
    status = "READY" if signal.ready and not findings_result.findings else "BLOCKED"
    lines = [
        "## Codex PR Review",
        "",
        f"Status: {status}",
        f"State: {signal.state}",
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
            "Comment on this PR with `@mb fix <comment-url>` or `/mb fix <comment-url>`.",
            f"[Open comment box]({compose_url})",
        ]
    )
    lines.extend(["", marker])
    return "\n".join(lines).strip()


def format_sticky_remediation_comment(
    *,
    issue_key: str | None,
    issue_url: str | None,
    issue_created: bool,
    enqueued: bool,
    reason: str | None,
    run_id: str | None,
    head_sha: str | None,
    event: str,
    action: str | None,
    marker: str,
) -> str:
    status = "ENQUEUED" if enqueued else ("PENDING" if issue_key else "BLOCKED")
    issue_reference = issue_key or "none"
    if issue_key and issue_url:
        issue_reference = f"[{issue_key}]({issue_url})"

    lines = [
        "## Codex PR Remediation",
        "",
        f"Status: {status}",
        f"Issue: {issue_reference}",
        f"Issue created: {'yes' if issue_created else 'no'}",
        f"Event: {event}/{str(action or 'none').strip() or 'none'}",
        f"Head SHA: {str(head_sha or '').strip() or 'unknown'}",
    ]
    if run_id:
        lines.append(f"Run ID: {run_id}")
    if reason:
        lines.append(f"Reason: {reason}")
    lines.extend(["", marker])
    return "\n".join(lines).strip()


def upsert_sticky_review_comment(
    *,
    github_client: GitHubAppClient,
    repo_full_name: str,
    pr_number: int,
    tenant_id: str,
    project_id: str,
    signal: ReviewerSignal,
    findings_result: PrReviewFindingsResult,
    event: str,
    action: str | None,
) -> StickyReviewCommentResult:
    marker = build_review_marker(
        tenant_id=tenant_id,
        project_id=project_id,
        repo_full_name=repo_full_name,
        pr_number=pr_number,
    )
    body = format_sticky_review_comment(
        signal=signal,
        findings_result=findings_result,
        repo_full_name=repo_full_name,
        pr_number=pr_number,
        event=event,
        action=action,
        marker=marker,
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


def upsert_sticky_remediation_comment(
    *,
    github_client: GitHubAppClient,
    repo_full_name: str,
    pr_number: int,
    tenant_id: str,
    project_id: str,
    issue_key: str | None,
    issue_url: str | None,
    issue_created: bool,
    enqueued: bool,
    reason: str | None,
    run_id: str | None,
    head_sha: str | None,
    event: str,
    action: str | None,
) -> StickyReviewCommentResult:
    marker = build_remediation_marker(
        tenant_id=tenant_id,
        project_id=project_id,
        repo_full_name=repo_full_name,
        pr_number=pr_number,
    )
    body = format_sticky_remediation_comment(
        issue_key=issue_key,
        issue_url=issue_url,
        issue_created=issue_created,
        enqueued=enqueued,
        reason=reason,
        run_id=run_id,
        head_sha=head_sha,
        event=event,
        action=action,
        marker=marker,
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
    github_client: GitHubAppClient,
    repo_full_name: str,
    pr_number: int,
    head_sha: str,
    findings: tuple[ReviewFinding, ...],
    changed_paths: set[str],
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

    review_result: PullRequestReviewSubmissionResult = github_client.submit_pull_request_review(
        repo_full_name=repo_full_name,
        pr_number=pr_number,
        commit_id=head_sha,
        body="Codex inline review findings.",
        comments=drafts,
    )
    return InlineReviewPublishResult(
        submitted=True,
        review_id=review_result.review_id,
        inline_count=len(drafts),
    )
