from __future__ import annotations

from dataclasses import dataclass
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.api.webhooks.pr_remediation_issue_service import (
    latest_issue_run as _latest_issue_run,
    repository_full_name as _repository_full_name,
)
from orchestrator.api.webhooks.pr_remediation_enqueue import enqueue_pr_remediation_run
from orchestrator.api.webhooks.pr_remediation_policy import (
    coerce_positive_int as _coerce_positive_int,
    is_remediation_trigger as _is_remediation_trigger,
    resolve_pr_remediation_issue_key,
)
from orchestrator.storage.models import Project, Run, Tenant
from orchestrator.tools.github_app import GitHubApiError, GitHubAppClient

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

    issue_key, issue_created, issue_error = resolve_pr_remediation_issue_key(
        session=session,
        tenant=tenant,
        project=project,
        github_client=github_client,
        settings=settings,
        repo_full_name=resolved_repo,
        pr_number=resolved_pr_number,
        head_sha=head_sha,
        head_ref=head_ref,
        pr_url=pr_url,
        title=title,
        body=body,
        event=normalized_event,
        action=normalized_action,
        checks=checks,
        reviews=reviews,
        review_comments=review_comments,
        issue_comments=issue_comments,
    )
    if issue_error is not None:
        return PrRemediationResult(
            triggered=True,
            issue_key=None,
            issue_created=False,
            enqueued=False,
            reason=issue_error,
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

    try:
        enqueue_result = enqueue_pr_remediation_run(
            session=session,
            tenant=tenant,
            project=project,
            issue_key=issue_key,
            issue_created=issue_created,
            pr_number=resolved_pr_number,
            details=details,
            normalized_event=normalized_event,
            normalized_action=normalized_action,
            checks=checks,
            reviews=reviews,
            review_comments=review_comments,
            issue_comments=issue_comments,
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
