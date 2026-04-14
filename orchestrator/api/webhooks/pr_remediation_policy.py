from __future__ import annotations

from dataclasses import dataclass

from orchestrator.api.webhooks.pr_remediation_issue_service import (
    create_pr_remediation_bug_issue_key,
    extract_issue_key,
    find_existing_issue_key_for_pr_head,
)
from orchestrator.tools.github_app import GitHubAppClient
from orchestrator.tools.jira_oauth import JiraOAuthError

@dataclass(frozen=True)
class ManualPrFixRequest:
    instruction_text: str
    parse_error: str | None


def parse_manual_pr_fix_request(*, payload: dict) -> ManualPrFixRequest | None:
    comment = payload.get("comment")
    body = str(comment.get("body") or "") if isinstance(comment, dict) else ""
    if not body.strip():
        return None
    first_non_empty_line = next((line.strip() for line in body.splitlines() if line.strip()), "")
    if not first_non_empty_line:
        return None
    normalized_line = first_non_empty_line.lower()
    prefix = next(
        (candidate for candidate in ("@mb", "/mb") if normalized_line.startswith(candidate)),
        None,
    )
    if prefix is None:
        return None
    instruction_lines = [first_non_empty_line[len(prefix):].strip()]
    instruction_lines.extend(line.strip() for line in body.splitlines()[1:] if line.strip())
    instruction_text = "\n".join(line for line in instruction_lines if line).strip()
    if not instruction_text:
        return ManualPrFixRequest(instruction_text="", parse_error="manual_fix_missing_instruction")
    return ManualPrFixRequest(instruction_text=instruction_text, parse_error=None)


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
    manual_fix_request: dict[str, object] | None = None,
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
            manual_fix_request=manual_fix_request,
        )
        issue_created = True
    except (JiraOAuthError, ValueError) as exc:
        return None, False, f"jira_bug_create_failed:{exc}"
    return issue_key, issue_created, None
