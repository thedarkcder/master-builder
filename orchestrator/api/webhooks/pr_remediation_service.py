from __future__ import annotations

from dataclasses import dataclass
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.api.jira_oauth.service import jira_oauth_client, refresh_jira_connection_tokens
from orchestrator.core.decision_engine import resolve_enqueue_precheck_outcome
from orchestrator.core.runs import enqueue_run
from orchestrator.storage.models import JiraOAuthConnection, Project, Run, Tenant
from orchestrator.tools.github_app import GitHubApiError, GitHubAppClient
from orchestrator.tools.jira_oauth import JiraIssueCreateInput, JiraOAuthError

_ISSUE_KEY_PATTERN = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
_STRICT_ISSUE_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")


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


def _extract_issue_key(*, texts: list[str]) -> str | None:
    for text in texts:
        match = _ISSUE_KEY_PATTERN.search(str(text or "").upper())
        if match:
            issue_key = _normalize_issue_key(match.group(1))
            if issue_key:
                return issue_key
    return None


def _repository_full_name(payload: dict) -> str:
    repository = payload.get("repository")
    if isinstance(repository, dict):
        full_name = str(repository.get("full_name") or "").strip()
        if full_name:
            return full_name
    raise ValueError("Missing repository full_name for remediation context")


def _coerce_positive_int(value: object | None) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return max(1, parsed)


def _normalize_issue_key(value: object | None) -> str | None:
    normalized = str(value or "").strip().upper()
    if not normalized:
        return None
    if not _STRICT_ISSUE_KEY_PATTERN.fullmatch(normalized):
        return None
    return normalized


def _find_existing_issue_key_for_pr_head(
    *,
    session: Session,
    tenant_id: str,
    project_id: str,
    pr_number: int,
    head_sha: str,
) -> str | None:
    normalized_head_sha = str(head_sha or "").strip()
    if not normalized_head_sha:
        return None
    runs = session.execute(
        select(Run)
        .where(
            Run.tenant_id == tenant_id,
            Run.project_id == project_id,
        )
        .order_by(Run.created_at.desc())
    ).scalars()
    for run in runs:
        issue_key = _normalize_issue_key(getattr(run, "issue_key", None))
        if issue_key is None:
            continue
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
        if str(trigger.get("head_sha") or "").strip() != normalized_head_sha:
            continue
        return issue_key
    return None


def _create_pr_remediation_bug_issue_key(
    *,
    session: Session,
    tenant: Tenant,
    project: Project,
    settings,  # noqa: ANN001
    repo_full_name: str,
    pr_number: int,
    pr_url: str | None,
    head_sha: str | None,
    event: str,
    action: str,
    checks: list,
    reviews: list,
    review_comments: list,
    issue_comments: list,
) -> str:
    connection_id = str((tenant.jira_config or {}).get("connection_id") or "").strip()
    if not connection_id:
        raise ValueError("jira_connection_missing")
    connection = session.get(JiraOAuthConnection, connection_id)
    if connection is None:
        raise ValueError(f"jira_connection_not_found:{connection_id}")

    access_token = refresh_jira_connection_tokens(
        session,
        connection=connection,
        settings=settings,
        tenant_id=tenant.tenant_id,
    )
    client = jira_oauth_client(
        session=session,
        settings=settings,
        tenant_id=tenant.tenant_id,
        project_id=project.project_id,
    )

    short_sha = str(head_sha or "").strip()[:12] or "unknown"
    summary = _truncate(
        f"PR remediation: {repo_full_name}#{pr_number} [{short_sha}]",
        limit=120,
    )
    description = _build_pr_remediation_bug_description(
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
    create_result = client.create_issues_bulk(
        access_token=access_token,
        cloud_id=connection.cloud_id,
        project_key=project.jira_project_key,
        issues=[
            JiraIssueCreateInput(
                summary=summary,
                description=description,
                labels=["codex-remediation", "github-pr", "automation"],
                issue_type="Bug",
            )
        ],
    )
    if not create_result.created:
        errors = "; ".join(create_result.errors) or "unknown error"
        raise ValueError(f"jira_bug_create_empty:{errors}")
    created_key = _normalize_issue_key(create_result.created[0].key)
    if created_key is None:
        raise ValueError("jira_bug_create_invalid_key")
    return created_key


def _build_pr_remediation_bug_description(
    *,
    repo_full_name: str,
    pr_number: int,
    pr_url: str | None,
    head_sha: str | None,
    event: str,
    action: str,
    checks: list,
    reviews: list,
    review_comments: list,
    issue_comments: list,
) -> str:
    lines = [
        "Automated bug created for PR remediation.",
        "",
        f"Repository: {repo_full_name}",
        f"PR: #{pr_number}",
        f"PR URL: {pr_url or 'unknown'}",
        f"Head SHA: {str(head_sha or '').strip() or 'unknown'}",
        f"Trigger: {event}/{action}",
    ]
    failing_checks = [
        f"{str(item.name or '').strip()}: {str(item.conclusion or item.status or '').strip() or 'unknown'}"
        for item in checks
        if str(getattr(item, "conclusion", "") or "").strip().lower() not in {"", "success", "neutral", "skipped"}
    ]
    if failing_checks:
        lines.extend(["", "Failing checks:"])
        lines.extend(f"- {_truncate(item, limit=220)}" for item in failing_checks[:8])

    changes_requested = [
        _truncate(str(item.body or "").replace("\n", " ").strip() or "changes requested", limit=240)
        for item in reviews
        if str(getattr(item, "state", "") or "").strip().upper() == "CHANGES_REQUESTED"
    ]
    if changes_requested:
        lines.extend(["", "Changes requested reviews:"])
        lines.extend(f"- {item}" for item in changes_requested[:5])

    if review_comments:
        lines.extend(["", "Review comments:"])
        for item in review_comments[:8]:
            path = str(getattr(item, "path", "") or "").strip() or "unknown"
            line_value = getattr(item, "line", None)
            location = f"{path}:{line_value}" if isinstance(line_value, int) and line_value > 0 else path
            body = _truncate(str(getattr(item, "body", "") or "").replace("\n", " ").strip(), limit=220)
            if body:
                lines.append(f"- {location} - {body}")
            else:
                lines.append(f"- {location}")

    if issue_comments:
        lines.extend(["", "Issue comments:"])
        for item in issue_comments[:5]:
            body = _truncate(str(getattr(item, "body", "") or "").replace("\n", " ").strip(), limit=220)
            if body:
                lines.append(f"- {body}")

    return "\n".join(lines).strip()


def _truncate(value: str, *, limit: int) -> str:
    normalized = str(value or "").strip()
    if len(normalized) <= limit:
        return normalized
    if limit <= 3:
        return normalized[:limit]
    return f"{normalized[: limit - 3].rstrip()}..."


def _latest_issue_run(
    *,
    session: Session,
    tenant_id: str,
    project_id: str | None,
    issue_key: str,
) -> Run | None:
    return (
        session.execute(
            select(Run)
            .where(
                Run.tenant_id == tenant_id,
                Run.project_id == project_id,
                Run.issue_key == issue_key,
            )
            .order_by(Run.created_at.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )
