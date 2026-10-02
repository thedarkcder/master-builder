from __future__ import annotations

import logging

from orchestrator.tools.repo_allowlist import normalize_repo_identifier


logger = logging.getLogger(__name__)


def normalize_branch(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def resolve_integration_branch(
    *,
    session,
    settings,
    tenant,
    run,
    project,
    base_branch: str,
    remediation_head_branch: str | None = None,
    github_client_from_tenant_config_fn,
    resolve_scoped_secret_ref_fn,
    resolve_platform_secret_ref_fn,
    resolve_branch_from_open_pull_requests_fn,
) -> str:
    if remediation_head_branch:
        run.branch = remediation_head_branch
        return remediation_head_branch

    run_branch = normalize_branch(getattr(run, "branch", None))
    if run_branch:
        return run_branch

    reused_branch = resolve_branch_from_open_pull_requests_fn(
        session=session,
        settings=settings,
        tenant=tenant,
        run=run,
        project=project,
        base_branch=base_branch,
        github_client_from_tenant_config_fn=github_client_from_tenant_config_fn,
        resolve_scoped_secret_ref_fn=resolve_scoped_secret_ref_fn,
        resolve_platform_secret_ref_fn=resolve_platform_secret_ref_fn,
    )
    integration_branch = reused_branch or f"feature/{run.issue_key}"
    run.branch = integration_branch
    return integration_branch


def resolve_branch_from_open_pull_requests(
    *,
    session,
    settings,
    tenant,
    run,
    project,
    base_branch: str,
    github_client_from_tenant_config_fn,
    resolve_scoped_secret_ref_fn,
    resolve_platform_secret_ref_fn,
) -> str | None:
    if project is None:
        return None
    github_repository = str(project.github_repository or "").strip()
    if not github_repository:
        return None
    github_config_raw = getattr(tenant, "github_config", {})
    github_config = github_config_raw if isinstance(github_config_raw, dict) else {}
    if not github_config:
        return None

    issue_key = str(run.issue_key or "").strip()
    if not issue_key:
        return None
    issue_key_lower = issue_key.lower()
    repo_full_name = repo_full_name_from_repository(github_repository)
    if repo_full_name is None:
        return None

    try:
        github_client = github_client_from_tenant_config_fn(
            github_config,
            tenant_secret_lookup=lambda secret_ref: resolve_scoped_secret_ref_fn(
                session,
                secret_ref=secret_ref,
                tenant_id=tenant.tenant_id,
                project_id=project.project_id,
                encryption_key=settings.secrets_encryption_key,
            ),
            platform_secret_lookup=lambda secret_ref: resolve_platform_secret_ref_fn(
                session,
                secret_ref=secret_ref,
                encryption_key=settings.secrets_encryption_key,
            ),
        )
        pull_requests = github_client.list_open_pull_requests(
            repo_full_name=repo_full_name, limit=100
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "workflow_branch_lookup_failed tenant_id=%s project_id=%s run_id=%s issue_key=%s error=%s",
            tenant.tenant_id,
            project.project_id,
            run.run_id,
            run.issue_key,
            exc,
        )
        return None

    for pull_request in pull_requests:
        if str(pull_request.base_ref or "").strip() != base_branch:
            continue
        title_lower = str(pull_request.title or "").lower()
        head_ref = normalize_branch(pull_request.head_ref)
        if not head_ref:
            continue
        head_lower = head_ref.lower()
        if issue_key_lower not in head_lower and issue_key_lower not in title_lower:
            continue
        return head_ref
    return None


def repo_full_name_from_repository(repository_url: str) -> str | None:
    normalized = normalize_repo_identifier(repository_url)
    prefix = "github.com/"
    if not normalized.startswith(prefix):
        return None
    full_name = normalized[len(prefix) :].strip("/")
    if full_name.count("/") != 1:
        return None
    return full_name
