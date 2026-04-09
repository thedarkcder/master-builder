from __future__ import annotations

from orchestrator.api.admin.project_normalization import normalize_project_discord_config
from orchestrator.api.run_schema_mappers import run_to_schema
from orchestrator.api.schemas import ProjectRead, RunRead, TenantRead, WorkflowRead
from orchestrator.core.config import get_settings
from orchestrator.core.project_policy import resolve_effective_policy
from orchestrator.storage.models import Project, Tenant, WorkflowExecution

__all__ = (
    "tenant_to_schema",
    "run_to_schema",
    "workflow_to_schema",
    "project_to_schema",
)


def tenant_to_schema(tenant: Tenant) -> TenantRead:
    return TenantRead(
        tenant_id=tenant.tenant_id,
        name=tenant.name,
        is_enabled=tenant.is_enabled,
        archived_at=tenant.archived_at,
        purge_after_at=tenant.purge_after_at,
        jira=tenant.jira_config,
        github=tenant.github_config,
        repos=tenant.repos_config,
        policy=tenant.policy_config,
        discord=tenant.discord_config,
        experience=tenant.experience_config or {},
        setup_state=tenant.setup_state or {},
        created_at=tenant.created_at,
        updated_at=tenant.updated_at,
    )


def workflow_to_schema(workflow: WorkflowExecution, *, runs: list[RunRead], pending_input_request_id: str | None, latest_checkpoint_kind: str | None) -> WorkflowRead:
    return WorkflowRead(
        workflow_id=workflow.workflow_id,
        tenant_id=workflow.tenant_id,
        project_id=workflow.project_id,
        issue_key=workflow.issue_key,
        issue_summary=workflow.issue_summary,
        repo_url=workflow.repo_url,
        branch=workflow.branch,
        pr_url=workflow.pr_url,
        dedupe_scope=workflow.dedupe_scope,
        status=workflow.status,
        active_run_id=workflow.active_run_id,
        latest_checkpoint_id=workflow.latest_checkpoint_id,
        source_workflow_id=workflow.source_workflow_id,
        source_run_id=workflow.source_run_id,
        blocked_reason=workflow.blocked_reason,
        pending_input_request_id=pending_input_request_id,
        latest_checkpoint_kind=latest_checkpoint_kind,
        runs=runs,
        created_at=workflow.created_at,
        started_at=workflow.started_at,
        finished_at=workflow.finished_at,
    )


def project_to_schema(project: Project, *, tenant_policy: dict) -> ProjectRead:
    normalized_project_discord = normalize_project_discord_config(project.discord_config)
    settings = get_settings()
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
            default_codex_model=settings.codex_model,
            default_codex_reasoning_effort=settings.codex_reasoning_effort,
        ),
        environment=project.environment,
        secret_refs=project.secret_refs,
        discord=normalized_project_discord if normalized_project_discord else None,
        is_archived=project.is_archived,
        created_at=project.created_at,
        updated_at=project.updated_at,
    )
