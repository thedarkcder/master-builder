from __future__ import annotations

import re

from sqlalchemy import select

from orchestrator.api.jira_oauth.service import jira_oauth_client, refresh_jira_connection_tokens
from orchestrator.storage.models import JiraOAuthConnection, Run
from orchestrator.tools.jira_oauth import JiraIssueCreateInput

_ISSUE_KEY_PATTERN = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
_STRICT_ISSUE_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")


def extract_issue_key(*, texts: list[str]) -> str | None:
    for text in texts:
        match = _ISSUE_KEY_PATTERN.search(str(text or "").upper())
        if match:
            issue_key = normalize_issue_key(match.group(1))
            if issue_key:
                return issue_key
    return None


def repository_full_name(payload: dict) -> str:
    repository = payload.get("repository")
    if isinstance(repository, dict):
        full_name = str(repository.get("full_name") or "").strip()
        if full_name:
            return full_name
    raise ValueError("Missing repository full_name for remediation context")


def normalize_issue_key(value: object | None) -> str | None:
    normalized = str(value or "").strip().upper()
    if not normalized:
        return None
    if not _STRICT_ISSUE_KEY_PATTERN.fullmatch(normalized):
        return None
    return normalized


def find_existing_issue_key_for_pr_head(
    *,
    session,
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
        issue_key = normalize_issue_key(getattr(run, "issue_key", None))
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


def create_pr_remediation_bug_issue_key(
    *,
    session,
    tenant,
    project,
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
    manual_fix_request: dict[str, object] | None = None,
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
    summary = truncate(
        f"PR remediation: {repo_full_name}#{pr_number} [{short_sha}]",
        limit=120,
    )
    description = build_pr_remediation_bug_description(
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
    created_key = normalize_issue_key(create_result.created[0].key)
    if created_key is None:
        raise ValueError("jira_bug_create_invalid_key")
    return created_key


def build_pr_remediation_bug_description(
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
    manual_fix_request: dict[str, object] | None = None,
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
    if isinstance(manual_fix_request, dict):
        lines.extend(["", "Manual request: yes"])
        requested_by = str(manual_fix_request.get("requested_by") or "").strip()
        if requested_by:
            lines.append(f"Requested by: {requested_by}")
        requested_comment = manual_fix_request.get("requested_comment")
        if isinstance(requested_comment, dict):
            comment_url = str(requested_comment.get("url") or "").strip()
            if comment_url:
                lines.append(f"Command comment: {comment_url}")
            comment_body = truncate(str(requested_comment.get("body") or "").replace("\n", " ").strip(), limit=240)
            if comment_body:
                lines.append(f"Comment body: {comment_body}")
        instruction_text = truncate(str(manual_fix_request.get("instruction_text") or "").strip(), limit=240)
        if instruction_text:
            lines.append(f"Instruction: {instruction_text}")
        code_context = manual_fix_request.get("code_context")
        if isinstance(code_context, dict):
            path = str(code_context.get("path") or "").strip() or "unknown"
            line_value = code_context.get("line")
            location = f"{path}:{line_value}" if isinstance(line_value, int) and line_value > 0 else path
            lines.extend(["", f"Referenced code: {location}"])
            snippet = str(code_context.get("snippet") or "").rstrip()
            if snippet:
                lines.append("```")
                lines.extend(snippet.splitlines())
                lines.append("```")
        return "\n".join(lines).strip()

    failing_checks = [
        f"{str(item.name or '').strip()}: {str(item.conclusion or item.status or '').strip() or 'unknown'}"
        for item in checks
        if str(getattr(item, "conclusion", "") or "").strip().lower() not in {"", "success", "neutral", "skipped"}
    ]
    if failing_checks:
        lines.extend(["", "Failing checks:"])
        lines.extend(f"- {truncate(item, limit=220)}" for item in failing_checks[:8])

    changes_requested = [
        truncate(str(item.body or "").replace("\n", " ").strip() or "changes requested", limit=240)
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
            body = truncate(str(getattr(item, "body", "") or "").replace("\n", " ").strip(), limit=220)
            if body:
                lines.append(f"- {location} - {body}")
            else:
                lines.append(f"- {location}")

    if issue_comments:
        lines.extend(["", "Issue comments:"])
        for item in issue_comments[:5]:
            body = truncate(str(getattr(item, "body", "") or "").replace("\n", " ").strip(), limit=220)
            if body:
                lines.append(f"- {body}")

    return "\n".join(lines).strip()


def truncate(value: str, *, limit: int) -> str:
    normalized = str(value or "").strip()
    if len(normalized) <= limit:
        return normalized
    if limit <= 3:
        return normalized[:limit]
    return f"{normalized[: limit - 3].rstrip()}..."


def latest_issue_run(
    *,
    session,
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
