from __future__ import annotations

from dataclasses import dataclass
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.api.webhooks.pr_remediation_issue_service import (
    create_pr_remediation_bug_issue_key as _create_pr_remediation_bug_issue_key,
    extract_issue_key as _extract_issue_key,
    find_existing_issue_key_for_pr_head as _find_existing_issue_key_for_pr_head,
    latest_issue_run as _latest_issue_run,
    repository_full_name as _repository_full_name,
)
from orchestrator.core.decision_engine import resolve_enqueue_precheck_outcome
from orchestrator.core.runs import enqueue_run
from orchestrator.storage.models import Project, Run, Tenant
from orchestrator.tools.github_app import GitHubApiError, GitHubAppClient
from orchestrator.tools.jira_oauth import JiraOAuthError

_ISSUE_KEY_PATTERN = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")


@dataclass(frozen=True)
class PrRemediationResult:
    triggered: bool
    issue_key: str | None
    issue_created: bool
    enqueued: bool
    reason: str | None
    run: Run | None
    head_sha: str | None


def enqueue_pr_remediation_if_needed(
    *,
    session: Session,
    tenant: Tenant,
    project: Project,
    github_client: GitHubAppClient,
    event: str,
    action: str | None,
    payload: dict,
    pr_number: int | None = None,
    repo_full_name: str | None = None,
    settings,  # noqa: ANN001
    max_concurrent_runs: int | None = None,
    max_attempts_per_head: int | None = None,
) -> PrRemediationResult:
    normalized_event = str(event or "").strip().lower()
    normalized_action = str(action or "").strip().lower()

    if not _is_remediation_trigger(event=normalized_event, action=normalized_action, payload=payload):
        return PrRemediationResult(
            triggered=False,
            issue_key=None,
            issue_created=False,
            enqueued=False,
            reason=None,
            run=None,
            head_sha=None,
        )

    pull_request = payload.get("pull_request")
    if isinstance(pull_request, dict):
        payload_pr_number = pull_request.get("number")
    else:
        payload_pr_number = None
    resolved_pr_number = (
        pr_number if isinstance(pr_number, int) and pr_number > 0 else payload_pr_number
    )
    if not isinstance(resolved_pr_number, int) or resolved_pr_number <= 0:
        return PrRemediationResult(
            triggered=True,
            issue_key=None,
            issue_created=False,
            enqueued=False,
            reason="missing_pr_number",
            run=None,
            head_sha=None,
        )

    resolved_repo = str(repo_full_name or "").strip()
    if not resolved_repo:
        try:
            resolved_repo = _repository_full_name(payload)
        except ValueError:
            resolved_repo = ""
    if not resolved_repo:
        return PrRemediationResult(
            triggered=True,
            issue_key=None,
            issue_created=False,
            enqueued=False,
            reason="missing_repository",
            run=None,
            head_sha=None,
        )

    details = None
    pr_url = None
    if isinstance(pull_request, dict):
        head = pull_request.get("head")
        head_sha = head.get("sha") if isinstance(head, dict) else None
        head_ref = head.get("ref") if isinstance(head, dict) else None
        base = pull_request.get("base")
        base_ref = base.get("ref") if isinstance(base, dict) else None
        pr_url_raw = pull_request.get("html_url")
        pr_url = str(pr_url_raw or "").strip() or None
        title = str(pull_request.get("title") or "").strip()
        body = str(pull_request.get("body") or "").strip()
    else:
        head_sha = None
        head_ref = None
        base_ref = None
        title = ""
        body = ""

    try:
        details = github_client.get_pull_request_details(
            repo_full_name=resolved_repo,
            pr_number=resolved_pr_number,
        )
    except (GitHubApiError, ValueError) as exc:
        return PrRemediationResult(
            triggered=True,
            issue_key=None,
            issue_created=False,
            enqueued=False,
            reason=f"pr_details_lookup_failed:{exc}",
            run=None,
            head_sha=str(head_sha or "").strip() or None,
        )

    head_sha = str(details.head_sha or head_sha or "").strip() or None
    head_ref = str(details.head_ref or head_ref or "").strip() or None
    base_ref = str(details.base_ref or base_ref or "").strip() or None
    title = str(details.title or title or "").strip()
    body = str(details.body or body or "").strip()
    pr_url = pr_url or str(details.html_url or "").strip() or None

    try:
        checks = github_client.list_check_suites(
            repo_full_name=resolved_repo,
            ref=str(details.head_sha or ""),
        )
        reviews = github_client.list_pull_request_reviews(
            repo_full_name=resolved_repo,
            pr_number=resolved_pr_number,
        )
        review_comments = github_client.list_pull_request_review_comments(
            repo_full_name=resolved_repo,
            pr_number=resolved_pr_number,
        )
        issue_comments = github_client.list_pull_request_issue_comments(
            repo_full_name=resolved_repo,
            pr_number=resolved_pr_number,
        )
    except (GitHubApiError, ValueError) as exc:
        return PrRemediationResult(
            triggered=True,
            issue_key=None,
            issue_created=False,
            enqueued=False,
            reason=f"github_context_fetch_failed:{exc}",
            run=None,
            head_sha=head_sha,
        )

    issue_key = _extract_issue_key(texts=[title, body, str(head_ref or "")])
    issue_created = False
    if issue_key is None and head_sha:
        issue_key = _find_existing_issue_key_for_pr_head(
            session=session,
            tenant_id=tenant.tenant_id,
            project_id=project.project_id,
            pr_number=resolved_pr_number,
            head_sha=head_sha,
        )
    if issue_key is None:
        try:
            issue_key = _create_pr_remediation_bug_issue_key(
                session=session,
                tenant=tenant,
                project=project,
                settings=settings,
                repo_full_name=resolved_repo,
                pr_number=resolved_pr_number,
                pr_url=pr_url,
                head_sha=head_sha,
                event=normalized_event,
                action=normalized_action,
                checks=checks,
                reviews=reviews,
                review_comments=review_comments,
                issue_comments=issue_comments,
            )
            issue_created = True
        except (JiraOAuthError, ValueError) as exc:
            return PrRemediationResult(
                triggered=True,
                issue_key=None,
                issue_created=False,
                enqueued=False,
                reason=f"jira_bug_create_failed:{exc}",
                run=None,
                head_sha=head_sha,
            )

    normalized_max_attempts = _coerce_positive_int(max_attempts_per_head)
    if normalized_max_attempts is not None and head_sha:
        attempt_count = count_pr_remediation_attempts(
            session=session,
            tenant_id=tenant.tenant_id,
            project_id=project.project_id,
            issue_key=issue_key,
            pr_number=resolved_pr_number,
            head_sha=head_sha,
        )
        if attempt_count >= normalized_max_attempts:
            latest_run = _latest_issue_run(
                session=session,
                tenant_id=tenant.tenant_id,
                project_id=project.project_id,
                issue_key=issue_key,
            )
            return PrRemediationResult(
                triggered=True,
                issue_key=issue_key,
                issue_created=issue_created,
                enqueued=False,
                reason="pr_remediation_attempt_limit_reached",
                run=latest_run,
                head_sha=head_sha,
            )

    trigger_context = {
        "source": "github_pr_review_feedback",
        "event": normalized_event,
        "action": normalized_action,
        "pr_number": resolved_pr_number,
        "pr_url": details.html_url,
        "head_sha": details.head_sha,
        "head_ref": details.head_ref or str(head_ref or ""),
        "base_ref": details.base_ref or str(base_ref or ""),
        "issue_key": issue_key,
        "issue_created": issue_created,
        "failing_checks": [
            {"name": check.name, "status": check.status, "conclusion": check.conclusion}
            for check in checks
            if check.conclusion not in {None, "success"}
        ],
        "changes_requested": [
            {
                "id": review.review_id,
                "state": review.state,
                "body": review.body,
                "user_login": review.user_login,
            }
            for review in reviews
            if review.state.strip().upper() == "CHANGES_REQUESTED"
        ],
        "review_comments": [
            {"id": comment.comment_id, "body": comment.body, "path": comment.path, "line": comment.line}
            for comment in review_comments
        ],
        "issue_comments": [
            {"id": comment.comment_id, "body": comment.body}
            for comment in issue_comments
        ],
    }

    try:
        enqueue_result = enqueue_run(
            session,
            tenant_id=tenant.tenant_id,
            project_id=project.project_id,
            issue_key=issue_key,
            issue_summary=f"{issue_key}: PR remediation for #{resolved_pr_number}",
            issue_description=(
                f"Automated remediation run triggered from GitHub PR #{resolved_pr_number} ({details.html_url}).\n"
                f"Event: {normalized_event}/{normalized_action}\n"
                f"Head SHA: {details.head_sha}"
            ),
            repo_url=project.github_repository,
            delivery_id=None,
            precheck_outcome=resolve_enqueue_precheck_outcome(source="github_pr_remediation"),
            max_concurrent_runs=max_concurrent_runs,
        )
    except ValueError as exc:
        return PrRemediationResult(
            triggered=True,
            issue_key=issue_key,
            issue_created=issue_created,
            enqueued=False,
            reason=f"enqueue_failed:{exc}",
            run=None,
            head_sha=head_sha,
        )
    run = enqueue_result.run
    if enqueue_result.enqueued:
        existing_plan = run.plan if isinstance(run.plan, dict) else {}
        run.plan = {
            **existing_plan,
            "trigger_context": trigger_context,
            "orchestration_mode": "orchestrated_subagents",
        }
        session.commit()
        session.refresh(run)
    return PrRemediationResult(
        triggered=True,
        issue_key=issue_key,
        issue_created=issue_created,
        enqueued=enqueue_result.enqueued,
        reason=enqueue_result.reason,
        run=run,
        head_sha=head_sha,
    )


def count_pr_remediation_attempts(
    *,
    session: Session,
    tenant_id: str,
    project_id: str | None,
    issue_key: str,
    pr_number: int,
    head_sha: str,
) -> int:
    runs = session.execute(
        select(Run).where(
            Run.tenant_id == tenant_id,
            Run.project_id == project_id,
            Run.issue_key == issue_key,
        )
    ).scalars()
    normalized_sha = str(head_sha or "").strip()
    total = 0
    for run in runs:
        plan = run.plan if isinstance(run.plan, dict) else {}
        trigger = plan.get("trigger_context") if isinstance(plan, dict) else None
        if not isinstance(trigger, dict):
            continue
        if str(trigger.get("source") or "").strip() != "github_pr_review_feedback":
            continue
        try:
            trigger_pr_number = int(trigger.get("pr_number") or 0)
        except (TypeError, ValueError):
            continue
        if trigger_pr_number != pr_number:
            continue
        if str(trigger.get("head_sha") or "").strip() != normalized_sha:
            continue
        total += 1
    return total


def _is_remediation_trigger(*, event: str, action: str, payload: dict) -> bool:
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
def _coerce_positive_int(value: object | None) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return max(1, parsed)
