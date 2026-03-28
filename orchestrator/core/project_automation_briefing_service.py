from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
from typing import Any
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from orchestrator.api.jira_oauth.connection_service import tenant_jira_oauth_context
from orchestrator.core.codex_invocation import CodexInvocationContext, invoke_codex_json
from orchestrator.core.codex_runtime import build_codex_runtime
from orchestrator.core.platform_secret_service import resolve_platform_secret_ref
from orchestrator.core.prompt_templates import render_prompt
from orchestrator.core.tenant_secret_service import resolve_scoped_secret_ref
from orchestrator.storage.models import Project, ProjectAutomation, Tenant
from orchestrator.tools.github_app import GitHubApiError, github_client_from_tenant_config


@dataclass(frozen=True)
class ProjectAutomationBriefing:
    transcript: str
    summary: str
    facts: dict[str, Any]


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _to_repo_full_name(repo_value: str) -> str:
    normalized = str(repo_value or "").strip()
    if not normalized:
        return ""
    if "/" in normalized and not normalized.startswith("http"):
        return normalized.strip("/")
    parsed = urlparse(normalized)
    path = str(parsed.path or "").strip("/")
    return path


def _format_jql_timestamp(value: datetime) -> str:
    return _to_utc(value).strftime("%Y-%m-%d %H:%M")


def _parse_timestamp(value: str | None) -> datetime | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    try:
        return datetime.fromisoformat(normalized.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _in_window(*, value: str | None, start: datetime, end: datetime) -> datetime | None:
    parsed = _parse_timestamp(value)
    if parsed is None:
        return None
    if start <= parsed <= end:
        return parsed
    return None


def _collect_jira_facts(
    *,
    session: Session,
    settings,
    tenant: Tenant,
    project: Project,
    window_start_at: datetime,
    window_end_at: datetime,
) -> dict[str, Any]:
    oauth = tenant_jira_oauth_context(session=session, tenant=tenant, settings=settings)
    jql = (
        f'project = "{project.jira_project_key}" '
        f'AND updated >= "{_format_jql_timestamp(window_start_at)}" '
        f'AND updated <= "{_format_jql_timestamp(window_end_at)}" '
        "ORDER BY updated DESC"
    )
    issues = oauth.client.search_issues_by_jql(
        access_token=oauth.access_token,
        cloud_id=oauth.connection.cloud_id,
        jql=jql,
        max_results=25,
    )
    return {
        "jql": jql,
        "issue_count": len(issues),
        "issues": [{"key": issue.key, "summary": issue.summary, "status": issue.status} for issue in issues],
    }


def _collect_github_facts(
    *,
    session: Session,
    settings,
    tenant: Tenant,
    project: Project,
    window_start_at: datetime,
    window_end_at: datetime,
) -> dict[str, Any]:
    repo_full_name = _to_repo_full_name(project.github_repository)
    if not repo_full_name:
        return {"repo": "", "pull_request_count": 0, "prs_in_window": []}
    try:
        client = github_client_from_tenant_config(
            tenant.github_config or {},
            tenant_secret_lookup=lambda secret_ref: resolve_scoped_secret_ref(
                session,
                secret_ref=secret_ref,
                tenant_id=tenant.tenant_id,
                project_id=project.project_id,
                encryption_key=settings.secrets_encryption_key,
            ),
            platform_secret_lookup=lambda secret_ref: resolve_platform_secret_ref(
                session,
                secret_ref=secret_ref,
                encryption_key=settings.secrets_encryption_key,
            ),
        )
        prs = client.list_pull_requests(repo_full_name=repo_full_name, state="all", limit=50)
    except Exception:
        return {"repo": repo_full_name, "pull_request_count": 0, "prs_in_window": []}
    start = _to_utc(window_start_at)
    end = _to_utc(window_end_at)
    in_window: list[dict[str, Any]] = []
    for pr in prs:
        created_at = _in_window(value=pr.created_at, start=start, end=end)
        updated_at = _in_window(value=pr.updated_at, start=start, end=end)
        closed_at = _in_window(value=pr.closed_at, start=start, end=end)
        merged_at = _in_window(value=pr.merged_at, start=start, end=end)
        if not any((created_at, updated_at, closed_at, merged_at)):
            continue
        reviews = []
        for review in client.list_pull_request_reviews(repo_full_name=repo_full_name, pr_number=pr.number):
            submitted_at = _in_window(value=review.submitted_at, start=start, end=end)
            if submitted_at is None:
                continue
            reviews.append(
                {
                    "review_id": review.review_id,
                    "state": review.state,
                    "submitted_at": submitted_at.isoformat(),
                    "user_login": review.user_login,
                }
            )
        review_comments = []
        for comment in client.list_pull_request_review_comments(repo_full_name=repo_full_name, pr_number=pr.number):
            created = _in_window(value=comment.created_at, start=start, end=end)
            if created is None:
                continue
            review_comments.append(
                {
                    "comment_id": comment.comment_id,
                    "created_at": created.isoformat(),
                    "path": comment.path,
                    "line": comment.line,
                    "state": comment.state,
                    "user_login": comment.user_login,
                }
            )
        issue_comments = []
        for comment in client.list_pull_request_issue_comments(repo_full_name=repo_full_name, pr_number=pr.number):
            created = _in_window(value=comment.created_at, start=start, end=end)
            if created is None:
                continue
            issue_comments.append(
                {
                    "comment_id": comment.comment_id,
                    "created_at": created.isoformat(),
                    "user_login": comment.user_login,
                }
            )
        if not any((created_at, updated_at, closed_at, merged_at, reviews, review_comments, issue_comments)):
            continue
        in_window.append(
            {
                "number": pr.number,
                "title": pr.title,
                "state": pr.state,
                "url": pr.html_url,
                "created_at": created_at.isoformat() if created_at is not None else None,
                "updated_at": updated_at.isoformat() if updated_at is not None else None,
                "closed_at": closed_at.isoformat() if closed_at is not None else None,
                "merged_at": merged_at.isoformat() if merged_at is not None else None,
                "review_count": len(reviews),
                "review_comment_count": len(review_comments),
                "issue_comment_count": len(issue_comments),
                "reviews": reviews,
                "review_comments": review_comments,
                "issue_comments": issue_comments,
            }
        )
    return {
        "repo": repo_full_name,
        "pull_request_count": len(in_window),
        "prs_in_window": in_window,
    }


def build_project_automation_briefing(
    *,
    session: Session,
    settings,
    tenant: Tenant,
    project: Project,
    automation: ProjectAutomation,
    window_start_at: datetime,
    window_end_at: datetime,
) -> ProjectAutomationBriefing:
    jira_facts = _collect_jira_facts(
        session=session,
        settings=settings,
        tenant=tenant,
        project=project,
        window_start_at=window_start_at,
        window_end_at=window_end_at,
    )
    github_facts = _collect_github_facts(
        session=session,
        settings=settings,
        tenant=tenant,
        project=project,
        window_start_at=window_start_at,
        window_end_at=window_end_at,
    )
    facts = {
        "kind": automation.kind,
        "window_start_at": _to_utc(window_start_at).isoformat(),
        "window_end_at": _to_utc(window_end_at).isoformat(),
        "jira": jira_facts,
        "github": github_facts,
    }
    runtime = build_codex_runtime(session=session, settings=settings)
    payload = invoke_codex_json(
        runtime=runtime,
        context=CodexInvocationContext(
            channel="system",
            tenant_id=tenant.tenant_id,
            project_id=project.project_id,
            command="project_automation",
            stage=automation.kind,
            working_dir=".",
            issue_key=None,
            reasoning_effort="medium",
        ),
        system_prompt=render_prompt(f"workflow/{automation.kind}_system.j2"),
        user_prompt=render_prompt(
            f"workflow/{automation.kind}_user.j2",
            project_name=project.name,
            jira_project_key=project.jira_project_key,
            github_repository=project.github_repository,
            window_start_at=_to_utc(window_start_at).isoformat(),
            window_end_at=_to_utc(window_end_at).isoformat(),
            facts_json=json.dumps(facts, sort_keys=True),
        ),
    )
    transcript = " ".join(str(payload.get("transcript") or "").split())
    summary = " ".join(str(payload.get("summary") or "").split())
    if not transcript:
        raise ValueError("Project automation briefing returned empty transcript")
    if not summary:
        summary = transcript
    return ProjectAutomationBriefing(transcript=transcript, summary=summary, facts=facts)


def safe_build_project_automation_briefing(
    *,
    session: Session,
    settings,
    tenant: Tenant,
    project: Project,
    automation: ProjectAutomation,
    window_start_at: datetime,
    window_end_at: datetime,
) -> ProjectAutomationBriefing:
    try:
        return build_project_automation_briefing(
            session=session,
            settings=settings,
            tenant=tenant,
            project=project,
            automation=automation,
            window_start_at=window_start_at,
            window_end_at=window_end_at,
        )
    except (GitHubApiError, RuntimeError, ValueError) as exc:
        fallback_transcript = (
            "AI-generated, source-derived project briefing. "
            f"Window {_to_utc(window_start_at).isoformat()} to {_to_utc(window_end_at).isoformat()}. "
            f"Could not build full specialist summary: {exc}."
        )
        return ProjectAutomationBriefing(
            transcript=fallback_transcript,
            summary=fallback_transcript,
            facts={
                "fallback": True,
                "error": str(exc),
                "window_start_at": _to_utc(window_start_at).isoformat(),
                "window_end_at": _to_utc(window_end_at).isoformat(),
            },
        )
