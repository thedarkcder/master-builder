from __future__ import annotations

from orchestrator.api.admin.project_normalization import normalize_project_discord_config
from orchestrator.api.schemas import (
    ProjectInstallRead,
    ProjectInstallRequestRead,
    ProjectRead,
    RunRead,
    TenantRead,
    WorkflowRead,
)
from orchestrator.core.config import get_settings
from orchestrator.core.project_policy import resolve_effective_policy
from orchestrator.storage.models import Project, ProjectInstall, ProjectInstallRequest, Run, Tenant, WorkflowExecution


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


def run_to_schema(run: Run) -> RunRead:
    return RunRead(
        run_id=run.run_id,
        workflow_id=run.workflow_id,
        attempt_number=run.attempt_number,
        parent_run_id=run.parent_run_id,
        entry_mode=run.entry_mode,
        entry_stage=run.entry_stage,
        entry_checkpoint_id=run.entry_checkpoint_id,
        tenant_id=run.tenant_id,
        project_id=run.project_id,
        issue_key=run.issue_key,
        issue_summary=run.issue_summary,
        issue_url=None,
        repo_url=run.repo_url,
        branch=run.branch,
        pr_url=run.pr_url,
        status=run.status,
        waiting_for_input=run.status == "waiting_for_input",
        pending_input_request_id=None,
        last_error=None if run.status == "succeeded" else run.last_error,
        plan=run.plan,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
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
            default_codex_model=None,
            default_codex_reasoning_effort=settings.codex_reasoning_effort,
        ),
        environment=project.environment,
        secret_refs=project.secret_refs,
        discord=normalized_project_discord if normalized_project_discord else None,
        is_archived=project.is_archived,
        created_at=project.created_at,
        updated_at=project.updated_at,
    )


def project_install_to_schema(install: ProjectInstall) -> ProjectInstallRead:
    return ProjectInstallRead(
        install_id=install.install_id,
        tenant_id=install.tenant_id,
        project_id=install.project_id,
        kind=install.kind,
        label=install.label,
        enabled=install.enabled,
        config=install.config_json if isinstance(install.config_json, dict) else {},
        binding_names=[str(item or "").strip() for item in list(install.binding_names_json or []) if str(item or "").strip()],
        created_at=install.created_at,
        updated_at=install.updated_at,
    )


def project_install_request_to_schema(request: ProjectInstallRequest) -> ProjectInstallRequestRead:
    return ProjectInstallRequestRead(
        request_id=request.request_id,
        tenant_id=request.tenant_id,
        project_id=request.project_id,
        workflow_id=request.workflow_id,
        run_id=request.run_id,
        issue_key=request.issue_key,
        kind=request.kind,
        label=request.label,
        reason=request.reason,
        suggested_config=request.suggested_config_json if isinstance(request.suggested_config_json, dict) else {},
        required_bindings=[
            str(item or "").strip() for item in list(request.required_bindings_json or []) if str(item or "").strip()
        ],
        status=request.status,
        request_kind=request.request_kind,
        created_at=request.created_at,
        updated_at=request.updated_at,
    )
