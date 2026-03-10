from __future__ import annotations

from orchestrator.api.webhooks.pr_remediation_issue_service import (
    create_pr_remediation_bug_issue_key,
    extract_issue_key,
    find_existing_issue_key_for_pr_head,
)
from orchestrator.tools.github_app import GitHubAppClient
from orchestrator.tools.jira_oauth import JiraOAuthError


def is_remediation_trigger(*, event: str, action: str, payload: dict) -> bool:
    if event == "pull_request_review" and action == "submitted":
        review = payload.get("review")
        state = str(review.get("state") or "").strip().lower() if isinstance(review, dict) else ""
        return state == "changes_requested"
    if event == "pull_request_review_comment" and action in {"created", "edited"}:
        return True
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
