from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.core.decision_engine import resolve_enqueue_precheck_outcome
from orchestrator.core.runs import EnqueueRunResult, enqueue_run
from orchestrator.storage.models import Project, Run, Tenant
from orchestrator.tools.github_app import GitHubAppClient

_ISSUE_KEY_PATTERN = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")


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
    max_concurrent_runs: int | None = None,
    max_attempts_per_head: int | None = None,
) -> EnqueueRunResult | None:
    normalized_event = str(event or "").strip().lower()
    normalized_action = str(action or "").strip().lower()

    if not _is_remediation_trigger(event=normalized_event, action=normalized_action, payload=payload):
        return None

    pull_request = payload.get("pull_request")
    if isinstance(pull_request, dict):
        payload_pr_number = pull_request.get("number")
    else:
        payload_pr_number = None
    resolved_pr_number = (
        pr_number if isinstance(pr_number, int) and pr_number > 0 else payload_pr_number
    )
    if not isinstance(resolved_pr_number, int) or resolved_pr_number <= 0:
        return None

    details = None
    if isinstance(pull_request, dict):
        head = pull_request.get("head")
        head_sha = head.get("sha") if isinstance(head, dict) else None
        head_ref = head.get("ref") if isinstance(head, dict) else None
        base = pull_request.get("base")
        base_ref = base.get("ref") if isinstance(base, dict) else None
        title = str(pull_request.get("title") or "").strip()
        body = str(pull_request.get("body") or "").strip()
    else:
        resolved_repo = str(repo_full_name or _repository_full_name(payload)).strip()
        if not resolved_repo:
            return None
        details = github_client.get_pull_request_details(
            repo_full_name=resolved_repo,
            pr_number=resolved_pr_number,
        )
        head_sha = details.head_sha
        head_ref = details.head_ref
        base_ref = details.base_ref
        title = details.title
        body = details.body or ""

    issue_key = _extract_issue_key(texts=[title, body, str(head_ref or "")])
    if issue_key is None:
        return None

    normalized_max_attempts = _coerce_positive_int(max_attempts_per_head)
    if normalized_max_attempts is not None and isinstance(head_sha, str) and head_sha.strip():
        attempt_count = count_pr_remediation_attempts(
            session=session,
            tenant_id=tenant.tenant_id,
            project_id=project.project_id,
            issue_key=issue_key,
            pr_number=resolved_pr_number,
            head_sha=head_sha.strip(),
        )
        if attempt_count >= normalized_max_attempts:
            latest_run = _latest_issue_run(
                session=session,
                tenant_id=tenant.tenant_id,
                project_id=project.project_id,
                issue_key=issue_key,
            )
            if latest_run is not None:
                return EnqueueRunResult(
                    enqueued=False,
                    reason="pr_remediation_attempt_limit_reached",
                    run=latest_run,
                )
            return None

    if details is None:
        details = github_client.get_pull_request_details(
            repo_full_name=str(repo_full_name or _repository_full_name(payload)),
            pr_number=resolved_pr_number,
        )
    checks = github_client.list_check_suites(
        repo_full_name=str(repo_full_name or _repository_full_name(payload)),
        ref=details.head_sha,
    )
    reviews = github_client.list_pull_request_reviews(
        repo_full_name=str(repo_full_name or _repository_full_name(payload)),
        pr_number=resolved_pr_number,
    )
    review_comments = github_client.list_pull_request_review_comments(
        repo_full_name=str(repo_full_name or _repository_full_name(payload)),
        pr_number=resolved_pr_number,
    )
    issue_comments = github_client.list_pull_request_issue_comments(
        repo_full_name=str(repo_full_name or _repository_full_name(payload)),
        pr_number=resolved_pr_number,
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
    run = enqueue_result.run
    existing_plan = run.plan if isinstance(run.plan, dict) else {}
    run.plan = {
        **existing_plan,
        "trigger_context": trigger_context,
        "orchestration_mode": "one_shot_subagents",
    }
    session.commit()
    session.refresh(run)
    return enqueue_result


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
            return match.group(1)
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
