from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException, status

from orchestrator.api.schemas import ReleaseBootstrapReportRead, RepoBootstrapStateRead


def list_tenant_repo_bootstrap_states(
    *,
    session,
    tenant_id: str,
    tenant_model,
    list_repo_bootstrap_states_fn,
):  # noqa: ANN001
    tenant = session.get(tenant_model, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    states = list_repo_bootstrap_states_fn(session=session, tenant_id=tenant_id)
    return [
        RepoBootstrapStateRead(
            tenant_id=state.tenant_id,
            repo_url=state.repo_url,
            bootstrap_count=state.bootstrap_count,
            last_created_files=list(state.last_created_files),
            bootstrapped_at=state.bootstrapped_at,
            updated_at=state.updated_at,
        )
        for state in states
    ]


def get_release_bootstrap_report(
    *,
    session,
    tenant_id: str,
    tenant_model,
    release_bootstrap_report_from_config_fn,
):  # noqa: ANN001
    tenant = session.get(tenant_model, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    return release_bootstrap_report_from_config_fn(tenant_id=tenant_id, jira_config=dict(tenant.jira_config or {}))


def run_release_bootstrap(
    *,
    session,
    tenant_id: str,
    tenant_model,
    settings,
    required_statuses,
    compute_release_bootstrap_result_fn,
    refresh_atlassian_connection_tokens_fn,
    atlassian_oauth_client_fn,
):  # noqa: ANN001
    tenant = session.get(tenant_model, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    ok, checks, details, report_payload = compute_release_bootstrap_result_fn(
        session=session,
        tenant=tenant,
        tenant_id=tenant_id,
        settings=settings,
        required_statuses=required_statuses,
        refresh_atlassian_connection_tokens_fn=refresh_atlassian_connection_tokens_fn,
        atlassian_oauth_client_fn=atlassian_oauth_client_fn,
    )
    jira_config = dict(tenant.jira_config or {})
    jira_config["release_bootstrap"] = report_payload
    tenant.jira_config = jira_config
    tenant.updated_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(tenant)

    return ReleaseBootstrapReportRead(
        tenant_id=tenant_id,
        ok=ok,
        checks=checks,
        details=details,
        checked_at=str(report_payload.get("checked_at") or ""),
    )
