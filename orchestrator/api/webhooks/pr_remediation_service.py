from __future__ import annotations

from dataclasses import dataclass
import logging
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.api.webhooks.pr_remediation_issue_service import (
    latest_issue_run as _latest_issue_run,
    repository_full_name as _repository_full_name,
    run_matches_pr_remediation_head as _run_matches_pr_remediation_head,
)
from orchestrator.api.webhooks.pr_remediation_enqueue import enqueue_pr_remediation_run
from orchestrator.api.webhooks.pr_remediation_policy import (
    coerce_positive_int as _coerce_positive_int,
    is_remediation_trigger as _is_remediation_trigger,
    parse_manual_pr_fix_request,
    resolve_pr_remediation_issue_key,
)
from orchestrator.core.review.pr_review_findings import (
    evaluate_pr_review_findings,
    pr_review_findings_clear,
)
from orchestrator.storage.models import Project, Run, Tenant
from orchestrator.tools.github_app import GitHubApiError, GitHubAppClient

_ISSUE_KEY_PATTERN = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
_MANUAL_FIX_SNIPPET_RADIUS = 5
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PrRemediationResult:
    triggered: bool
    issue_key: str | None
    issue_created: bool
    enqueued: bool
    reason: str | None
    run: Run | None
    head_sha: str | None


def _should_skip_check_trigger_remediation(
    *,
    github_client: GitHubAppClient,
    repo_full_name: str,
    pr_number: int,
    pr_title: str,
    pr_body: str,
    checks: list,
    tenant_id: str,
    project_id: str,
) -> tuple[bool, str | None]:
    try:
        changed_files = github_client.list_pull_request_files(
            repo_full_name=repo_full_name,
            pr_number=pr_number,
        )
        findings_result = evaluate_pr_review_findings(
            repo_full_name=repo_full_name,
            pr_number=pr_number,
            pr_title=pr_title,
            pr_body=pr_body,
            workflow_checks=checks,
            changed_files=changed_files,
            tenant_id=tenant_id,
            project_id=project_id,
        )
    except Exception as exc:  # noqa: BLE001
        return True, f"review_findings_evaluation_failed:{exc}"
    if pr_review_findings_clear(findings_result):
        return True, "review_clear_no_actionable_findings"
    return False, None


def _extract_payload_comment(payload: dict) -> dict:
    comment = payload.get("comment")
    return comment if isinstance(comment, dict) else {}


def _extract_payload_comment_id(*, payload: dict) -> int | None:
    comment_id = _extract_payload_comment(payload).get("id")
    return comment_id if isinstance(comment_id, int) and comment_id > 0 else None


def _extract_payload_comment_login(*, payload: dict) -> str | None:
    user = _extract_payload_comment(payload).get("user")
    if not isinstance(user, dict):
        return None
    login = str(user.get("login") or "").strip()
    return login or None


def _extract_payload_comment_url(*, payload: dict) -> str | None:
    comment_url = str(_extract_payload_comment(payload).get("html_url") or "").strip()
    return comment_url or None


def _resolve_triggering_comment_payload(
    *, event: str, payload: dict
) -> dict[str, object] | None:
    comment = _extract_payload_comment(payload)
    comment_id = _extract_payload_comment_id(payload=payload)
    if comment_id is None:
        return None
    comment_url = _extract_payload_comment_url(payload=payload)
    comment_body = str(comment.get("body") or "").strip()
    if not comment_body:
        return None
    user_login = _extract_payload_comment_login(payload=payload)
    normalized_event = str(event or "").strip().lower()
    comment_type = (
        "review_comment"
        if normalized_event == "pull_request_review_comment"
        else "issue_comment"
    )
    resolved = {
        "type": comment_type,
        "id": comment_id,
        "url": comment_url,
        "body": comment_body,
        "user_login": user_login,
    }
    if comment_type == "review_comment":
        path = str(comment.get("path") or "").strip() or None
        line = comment.get("line")
        resolved["path"] = path
        resolved["line"] = line
    return resolved


def _resolve_manual_fix_code_context(
    *,
    github_client: GitHubAppClient,
    repo_full_name: str,
    head_sha: str | None,
    requested_comment: dict[str, object],
) -> tuple[dict[str, object] | None, str | None]:
    comment_type = str(requested_comment.get("type") or "").strip()
    if comment_type != "review_comment":
        return None, None
    path = str(requested_comment.get("path") or "").strip()
    line = requested_comment.get("line")
    normalized_head_sha = str(head_sha or "").strip()
    if not path:
        return None, "missing_file_path"
    if not isinstance(line, int) or line <= 0:
        return None, "missing_line"
    if not normalized_head_sha:
        return None, "missing_head_sha"
    try:
        content = github_client.get_file_text_at_ref(
            repo_full_name=repo_full_name,
            path=path,
            ref=normalized_head_sha,
        )
    except (GitHubApiError, ValueError) as exc:
        return None, f"content_lookup_failed:{exc}"
    lines = content.splitlines()
    if line > len(lines):
        return None, "line_out_of_range"
    start_line = max(1, line - _MANUAL_FIX_SNIPPET_RADIUS)
    end_line = min(len(lines), line + _MANUAL_FIX_SNIPPET_RADIUS)
    snippet = "\n".join(
        f"{number}: {lines[number - 1]}" for number in range(start_line, end_line + 1)
    )
    return (
        {
            "path": path,
            "line": line,
            "head_sha": normalized_head_sha,
            "start_line": start_line,
            "end_line": end_line,
            "snippet": snippet,
        },
        None,
    )


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
    manual_fix_request = parse_manual_pr_fix_request(payload=payload)

    if not _is_remediation_trigger(
        event=normalized_event, action=normalized_action, payload=payload
    ):
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

    requested_comment = None
    requested_by = _extract_payload_comment_login(payload=payload)
    triggering_comment_id = _extract_payload_comment_id(payload=payload)
    triggering_comment_url = _extract_payload_comment_url(payload=payload)
    if manual_fix_request is not None:
        if manual_fix_request.parse_error is not None:
            return PrRemediationResult(
                triggered=True,
                issue_key=None,
                issue_created=False,
                enqueued=False,
                reason=manual_fix_request.parse_error,
                run=None,
                head_sha=head_sha,
            )
        requested_comment = _resolve_triggering_comment_payload(
            event=normalized_event,
            payload=payload,
        )
        if requested_comment is None:
            return PrRemediationResult(
                triggered=True,
                issue_key=None,
                issue_created=False,
                enqueued=False,
                reason="manual_fix_missing_comment_payload",
                run=None,
                head_sha=head_sha,
            )
        code_context, code_context_resolution = _resolve_manual_fix_code_context(
            github_client=github_client,
            repo_full_name=resolved_repo,
            head_sha=head_sha,
            requested_comment=requested_comment,
        )
    else:
        code_context = None
        code_context_resolution = None

    if manual_fix_request is None and normalized_event in {"check_run", "check_suite"}:
        skip_remediation, skip_reason = _should_skip_check_trigger_remediation(
            github_client=github_client,
            repo_full_name=resolved_repo,
            pr_number=resolved_pr_number,
            pr_title=title,
            pr_body=body,
            checks=checks,
            tenant_id=tenant.tenant_id,
            project_id=project.project_id,
        )
        if skip_remediation:
            return PrRemediationResult(
                triggered=True,
                issue_key=None,
                issue_created=False,
                enqueued=False,
                reason=skip_reason,
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
        manual_fix_request=(
            {
                "requested_by": requested_by,
                "requested_comment": requested_comment,
                "triggering_comment_id": triggering_comment_id,
                "triggering_comment_url": triggering_comment_url,
                "instruction_text": manual_fix_request.instruction_text,
                "command": "mb",
                "code_context": code_context,
                "code_context_resolution": code_context_resolution,
            }
            if manual_fix_request is not None and requested_comment is not None
            else None
        ),
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

    if manual_fix_request is None and not issue_created:
        latest_run = _latest_issue_run(
            session=session,
            tenant_id=tenant.tenant_id,
            project_id=project.project_id,
            issue_key=issue_key,
        )
        return PrRemediationResult(
            triggered=True,
            issue_key=issue_key,
            issue_created=False,
            enqueued=False,
            reason="existing_issue_already_tracked",
            run=latest_run,
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
            manual_fix_request=(
                {
                    "requested_by": requested_by,
                    "requested_comment": requested_comment,
                    "triggering_comment_id": triggering_comment_id,
                    "triggering_comment_url": triggering_comment_url,
                    "instruction_text": manual_fix_request.instruction_text,
                    "command": "mb",
                    "code_context": code_context,
                    "code_context_resolution": code_context_resolution,
                }
                if requested_comment is not None
                else None
            ),
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
        try:
            if not _run_matches_pr_remediation_head(
                run=run,
                pr_number=pr_number,
                head_sha=normalized_sha,
            ):
                continue
        except ValueError as exc:
            logger.warning(
                "pr_remediation_attempt_count_invalid_snapshot_skipped tenant_id=%s project_id=%s run_id=%s issue_key=%s error=%s",
                tenant_id,
                project_id,
                getattr(run, "run_id", None),
                issue_key,
                exc,
            )
            continue
        total += 1
    return total
