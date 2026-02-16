from __future__ import annotations

from orchestrator.api.admin.project_normalization import normalize_project_discord_config
from orchestrator.api.schemas import ProjectRead, RunRead, TenantRead
from orchestrator.core.project_policy import resolve_effective_policy
from orchestrator.storage.models import Project, Run, Tenant


def tenant_to_schema(tenant: Tenant) -> TenantRead:
    return TenantRead(
        tenant_id=tenant.tenant_id,
        name=tenant.name,
        is_enabled=tenant.is_enabled,
        jira=tenant.jira_config,
        github=tenant.github_config,
        repos=tenant.repos_config,
        policy=tenant.policy_config,
        discord=tenant.discord_config,
        created_at=tenant.created_at,
        updated_at=tenant.updated_at,
    )


def run_to_schema(run: Run) -> RunRead:
    return RunRead(
        run_id=run.run_id,
        tenant_id=run.tenant_id,
        project_id=run.project_id,
        issue_key=run.issue_key,
        issue_summary=run.issue_summary,
        issue_url=None,
        repo_url=run.repo_url,
        branch=run.branch,
        pr_url=run.pr_url,
        status=run.status,
        last_error=None if run.status == "succeeded" else run.last_error,
        plan=run.plan,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


def project_to_schema(project: Project, *, tenant_policy: dict) -> ProjectRead:
    normalized_project_discord = normalize_project_discord_config(project.discord_config)
    return ProjectRead(
        project_id=project.project_id,
        tenant_id=project.tenant_id,
        name=project.name,
        github_repository=project.github_repository,
        jira_project_key=project.jira_project_key,
        policy_overrides=project.policy_overrides,
        effective_policy=resolve_effective_policy(
            tenant_policy=tenant_policy,
            project_overrides=project.policy_overrides,
        ),
        environment=project.environment,
        secret_refs=project.secret_refs,
        discord=normalized_project_discord if normalized_project_discord else None,
        is_archived=project.is_archived,
        created_at=project.created_at,
        updated_at=project.updated_at,
    )
