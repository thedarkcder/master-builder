from __future__ import annotations

from sqlalchemy import select

from orchestrator.core.config import get_settings
from orchestrator.core.platform.secret_service import resolve_platform_secret_ref
from orchestrator.core.platform.tenant_secret_service import resolve_scoped_secret_ref
from orchestrator.storage.models import Project, Tenant
from orchestrator.tools.github_app import (
    GitHubApiError,
    github_client_from_tenant_config,
)
from orchestrator.tools.project_repo_checkout import collect_local_repo_context
from orchestrator.tools.repo_allowlist import normalize_repo_identifier


def collect_project_repo_context_for_issue(
    *,
    session,
    tenant: Tenant,
    issue_key: str,
    find_active_project_for_issue_key_fn,
    get_settings_fn=get_settings,
    collect_local_repo_context_fn=collect_local_repo_context,
) -> dict:
    project = find_active_project_for_issue_key_fn(
        session,
        tenant_id=tenant.tenant_id,
        issue_key=issue_key,
    )
    if project is None:
        return {"available": False, "reason": "project_not_mapped"}
    settings = get_settings_fn()
    local_context = collect_local_repo_context_fn(
        base_dir=settings.project_repo_checkout_base_dir,
        tenant_id=tenant.tenant_id,
        project=project,
        issue_key=issue_key,
    )
    return {
        "available": local_context.available,
        "reason": local_context.reason,
        "repo_dir": local_context.repo_dir,
        "current_branch": local_context.current_branch,
        "head_sha": local_context.head_sha,
        "branches": local_context.branches,
        "recent_commits": local_context.recent_commits,
        "issue_related_commits": local_context.issue_related_commits,
    }


def repo_full_name_from_repository_url(repository_url: str) -> str | None:
    normalized_repo = normalize_repo_identifier(repository_url)
    github_prefix = "github.com/"
    if not normalized_repo.startswith(github_prefix):
        return None
    repo_full_name = normalized_repo[len(github_prefix) :].strip("/")
    if repo_full_name.count("/") != 1:
        return None
    return repo_full_name


def collect_github_ask_context(
    *,
    session,
    tenant: Tenant,
    project_keys: list[str],
    get_settings_fn=get_settings,
    resolve_scoped_secret_ref_fn=resolve_scoped_secret_ref,
    resolve_platform_secret_ref_fn=resolve_platform_secret_ref,
    github_client_from_tenant_config_fn=github_client_from_tenant_config,
    collect_local_repo_context_fn=collect_local_repo_context,
) -> dict:
    normalized_project_keys = {
        str(value).strip().upper() for value in project_keys if str(value).strip()
    }
    query = select(Project).where(
        Project.tenant_id == tenant.tenant_id,
        Project.is_archived.is_(False),
    )
    projects = session.execute(query).scalars().all()
    scoped_projects = [
        project
        for project in projects
        if not normalized_project_keys
        or project.jira_project_key.strip().upper() in normalized_project_keys
    ]
    if not scoped_projects:
        return {"available": False, "reason": "no_active_projects", "repositories": []}

    repo_map: dict[str, dict] = {}
    for project in scoped_projects:
        repo_full_name = repo_full_name_from_repository_url(project.github_repository)
        if not repo_full_name:
            continue
        entry = repo_map.setdefault(
            repo_full_name,
            {
                "repo_full_name": repo_full_name,
                "project_keys": [],
                "open_pull_requests": [],
                "staging_pull_requests": [],
                "local_repo": {},
            },
        )
        project_key = project.jira_project_key.strip().upper()
        if project_key and project_key not in entry["project_keys"]:
            entry["project_keys"].append(project_key)

    if not repo_map:
        return {
            "available": False,
            "reason": "no_github_repositories",
            "repositories": [],
        }

    settings = get_settings_fn()
    for project in scoped_projects:
        repo_full_name = repo_full_name_from_repository_url(project.github_repository)
        if not repo_full_name or repo_full_name not in repo_map:
            continue
        local_context = collect_local_repo_context_fn(
            base_dir=settings.project_repo_checkout_base_dir,
            tenant_id=tenant.tenant_id,
            project=project,
        )
        repo_map[repo_full_name]["local_repo"] = {
            "available": local_context.available,
            "reason": local_context.reason,
            "repo_dir": local_context.repo_dir,
            "current_branch": local_context.current_branch,
            "head_sha": local_context.head_sha,
            "branches": local_context.branches,
            "recent_commits": local_context.recent_commits,
        }

    try:
        github_client = github_client_from_tenant_config_fn(
            tenant.github_config,
            tenant_secret_lookup=lambda secret_ref: resolve_scoped_secret_ref_fn(
                session,
                secret_ref=secret_ref,
                encryption_key=settings.secrets_encryption_key,
                tenant_id=tenant.tenant_id,
            ),
            platform_secret_lookup=lambda secret_ref: resolve_platform_secret_ref_fn(
                session,
                secret_ref=secret_ref,
                encryption_key=settings.secrets_encryption_key,
            ),
        )
    except ValueError as exc:
        return {
            "available": False,
            "reason": f"github_client_unavailable:{exc}",
            "repositories": list(repo_map.values()),
        }

    repo_contexts: list[dict] = []
    degraded_repositories: list[str] = []
    for repo_full_name in sorted(repo_map.keys())[:5]:
        repo_entry = repo_map[repo_full_name]
        try:
            pull_requests = github_client.list_open_pull_requests(
                repo_full_name=repo_full_name,
                limit=15,
            )
        except (GitHubApiError, ValueError):
            pull_requests = []
            degraded_repositories.append(repo_full_name)
        repo_entry["open_pull_requests"] = [
            {
                "number": pr.number,
                "title": pr.title,
                "state": pr.state,
                "head_ref": pr.head_ref,
                "base_ref": pr.base_ref,
                "html_url": pr.html_url,
                "updated_at": pr.updated_at,
            }
            for pr in pull_requests
        ]
        repo_entry["staging_pull_requests"] = [
            pr_entry
            for pr_entry in repo_entry["open_pull_requests"]
            if str(pr_entry.get("base_ref") or "").strip().lower() == "staging"
        ]
        repo_contexts.append(repo_entry)
    if degraded_repositories:
        return {
            "available": False,
            "reason": "github_pull_requests_unavailable",
            "degraded_repositories": degraded_repositories,
            "repositories": repo_contexts,
        }
    return {"available": True, "repositories": repo_contexts}
