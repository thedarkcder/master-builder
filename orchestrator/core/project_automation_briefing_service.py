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
        return {"repo": "", "open_pr_count": 0, "prs_in_window": []}
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
        prs = client.list_open_pull_requests(repo_full_name=repo_full_name, limit=50)
    except Exception:
        return {"repo": repo_full_name, "open_pr_count": 0, "prs_in_window": []}
    start = _to_utc(window_start_at)
    end = _to_utc(window_end_at)
    in_window: list[dict[str, Any]] = []
    for pr in prs:
        updated_at_raw = str(pr.updated_at or "").strip()
        if not updated_at_raw:
            continue
        try:
            updated = datetime.fromisoformat(updated_at_raw.replace("Z", "+00:00")).astimezone(UTC)
        except ValueError:
            continue
        if start <= updated <= end:
            in_window.append(
                {
                    "number": pr.number,
                    "title": pr.title,
                    "state": pr.state,
                    "url": pr.html_url,
                    "updated_at": updated.isoformat(),
                }
            )
    return {
        "repo": repo_full_name,
        "open_pr_count": len(prs),
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
