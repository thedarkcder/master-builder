from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import urlparse

from orchestrator.api.webhooks.pr_remediation_issue_service import (
    create_pr_remediation_bug_issue_key,
    extract_issue_key,
    find_existing_issue_key_for_pr_head,
)
from orchestrator.tools.github_app import GitHubAppClient
from orchestrator.tools.jira_oauth import JiraOAuthError

_MANUAL_FIX_COMMAND_PATTERN = re.compile(
    r"^\s*(?:@mb|/mb)\s+fix(?:\s+(?P<comment_url>\S+))?\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_PULL_COMMENT_URL_PATTERN = re.compile(r"^/([^/]+/[^/]+)/pull/(\d+)(?:/(?:files|commits|checks))?$")
_DISCUSSION_ANCHOR_PATTERN = re.compile(r"^discussion_r(?P<comment_id>\d+)$")
_ISSUE_COMMENT_ANCHOR_PATTERN = re.compile(r"^issuecomment-(?P<comment_id>\d+)$")


@dataclass(frozen=True)
class ManualPrFixRequest:
    command: str
    comment_url: str | None
    parse_error: str | None


@dataclass(frozen=True)
class RequestedCommentRef:
    comment_type: str
    comment_id: int
    comment_url: str


def parse_manual_pr_fix_request(*, payload: dict) -> ManualPrFixRequest | None:
    comment = payload.get("comment")
    body = str(comment.get("body") or "") if isinstance(comment, dict) else ""
    if not body.strip():
        return None
    match = _MANUAL_FIX_COMMAND_PATTERN.search(body)
    if match is None:
        return None
    comment_url = str(match.group("comment_url") or "").strip() or None
    return ManualPrFixRequest(command="fix", comment_url=comment_url, parse_error=None)


def resolve_requested_comment_ref(
    *,
    comment_url: str,
    repo_full_name: str,
    pr_number: int,
) -> tuple[RequestedCommentRef | None, str | None]:
    parsed_url = urlparse(comment_url)
    normalized_path = parsed_url.path.strip()
    match = _PULL_COMMENT_URL_PATTERN.match(normalized_path)
    if match is None:
        return None, "manual_fix_invalid_comment_url"
    url_repo = str(match.group(1) or "").strip()
    url_pr_raw = str(match.group(2) or "").strip()
    if url_repo.lower() != repo_full_name.lower():
        return None, "manual_fix_comment_url_repo_mismatch"
    try:
        url_pr_number = int(url_pr_raw)
    except ValueError:
        return None, "manual_fix_invalid_comment_url"
    if url_pr_number != pr_number:
        return None, "manual_fix_comment_url_pr_mismatch"
    anchor = str(parsed_url.fragment or "").strip()
    if not anchor:
        return None, "manual_fix_comment_url_missing_anchor"
    discussion_match = _DISCUSSION_ANCHOR_PATTERN.match(anchor)
    if discussion_match is not None:
        return RequestedCommentRef(
            comment_type="review_comment",
            comment_id=int(discussion_match.group("comment_id")),
            comment_url=comment_url,
        ), None
    issue_comment_match = _ISSUE_COMMENT_ANCHOR_PATTERN.match(anchor)
    if issue_comment_match is not None:
        return RequestedCommentRef(
            comment_type="issue_comment",
            comment_id=int(issue_comment_match.group("comment_id")),
            comment_url=comment_url,
        ), None
    return None, "manual_fix_unsupported_comment_anchor"


def is_remediation_trigger(*, event: str, action: str, payload: dict) -> bool:
    if event == "pull_request_review" and action == "submitted":
        review = payload.get("review")
        state = str(review.get("state") or "").strip().lower() if isinstance(review, dict) else ""
        return state == "changes_requested"
    if event == "pull_request_review_comment" and action in {"created", "edited"}:
        return True
    if event == "issue_comment" and action in {"created", "edited"}:
        return parse_manual_pr_fix_request(payload=payload) is not None
    if event == "check_run" and action in {"created", "completed", "rerequested"}:
        check_run = payload.get("check_run")
        conclusion = str(check_run.get("conclusion") or "").strip().lower() if isinstance(check_run, dict) else ""
        return conclusion not in {"", "success", "neutral", "skipped"}
    if event == "check_suite" and action in {"completed", "requested", "rerequested"}:
        check_suite = payload.get("check_suite")
        conclusion = str(check_suite.get("conclusion") or "").strip().lower() if isinstance(check_suite, dict) else ""
        return conclusion not in {"", "success", "neutral", "skipped"}
    return False



def coerce_positive_int(value: object | None) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return max(1, parsed)



def resolve_pr_remediation_issue_key(
    *,
    session,
    tenant,
    project,
    github_client: GitHubAppClient,
    settings,
    repo_full_name: str,
    pr_number: int,
    head_sha: str | None,
    head_ref: str | None,
    pr_url: str | None,
    title: str,
    body: str,
    event: str,
    action: str,
    checks,
    reviews,
    review_comments,
    issue_comments,
) -> tuple[str | None, bool, str | None]:
    issue_key = extract_issue_key(texts=[title, body, str(head_ref or "")])
    issue_created = False
    if issue_key is None and head_sha:
        issue_key = find_existing_issue_key_for_pr_head(
            session=session,
            tenant_id=tenant.tenant_id,
            project_id=project.project_id,
            pr_number=pr_number,
            head_sha=head_sha,
        )
    if issue_key is not None:
        return issue_key, issue_created, None

    try:
        issue_key = create_pr_remediation_bug_issue_key(
            session=session,
            tenant=tenant,
            project=project,
            settings=settings,
            repo_full_name=repo_full_name,
            pr_number=pr_number,
            pr_url=pr_url,
            head_sha=head_sha,
            event=event,
            action=action,
            checks=checks,
            reviews=reviews,
            review_comments=review_comments,
            issue_comments=issue_comments,
        )
        issue_created = True
    except (JiraOAuthError, ValueError) as exc:
        return None, False, f"jira_bug_create_failed:{exc}"
    return issue_key, issue_created, None
