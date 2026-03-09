from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from orchestrator.api.dependencies import get_session
from orchestrator.api.admin.discord_allowlist_helpers import (
    notify_discord_allowlist_approved as _notify_discord_allowlist_approved_core,
)
from orchestrator.api.admin.jira_webhook_helpers import (
    default_ready_jql as _default_ready_jql,
    jira_webhook_callback_url as _jira_webhook_callback_url,
    jira_webhook_filter_jql as _jira_webhook_filter_jql,
    is_jira_webhook_limit_error as _is_jira_webhook_limit_error,
    is_jira_webhook_single_url_error as _is_jira_webhook_single_url_error,
    extract_jira_webhook_conflict_url as _extract_jira_webhook_conflict_url,
    parse_jira_webhook_id as _parse_jira_webhook_id,
    parse_managed_webhook_ids as _parse_managed_webhook_ids,
)
from orchestrator.api.admin.jira_webhook_delete import (
    delete_jira_webhooks as _delete_jira_webhooks_core,
)
from orchestrator.api.admin.jira_webhook_provision import (
    provision_jira_webhook as _provision_jira_webhook_core,
)
from orchestrator.api.admin.release_bootstrap_helpers import (
    compute_release_bootstrap_result as _compute_release_bootstrap_result_impl,
    release_bootstrap_report_from_config as _release_bootstrap_report_from_config_impl,
)
from orchestrator.api.admin.release_bootstrap_service import (
    get_release_bootstrap_report as _get_release_bootstrap_report_impl,
    list_tenant_repo_bootstrap_states as _list_tenant_repo_bootstrap_states_impl,
    run_release_bootstrap as _run_release_bootstrap_impl,
)
from orchestrator.api.admin.route_helpers import (
    cleanup_conflicting_jira_webhook_url as _cleanup_conflicting_jira_webhook_url,
    cleanup_unmanaged_jira_webhooks_for_connection as _cleanup_unmanaged_jira_webhooks_for_connection,
    jira_oauth_client as _jira_oauth_client_impl,
    jira_webhook_action_status_code as _jira_webhook_action_status_code,
    parse_discord_allowlist_requests as _parse_discord_allowlist_requests,
    refresh_jira_connection_tokens as _refresh_jira_connection_tokens_impl,
    remove_managed_webhook_id_from_tenants as _remove_managed_webhook_id_from_tenants,
    with_managed_github_refs as _with_managed_github_refs,
)
from orchestrator.api.admin.jira_connect_flow import (
    build_jira_connect_start as _build_jira_connect_start_impl,
    handle_jira_connect_callback as _handle_jira_connect_callback_impl,
)
from orchestrator.api.admin.github_helpers import (
    github_install_callback as _github_install_callback_impl,
    list_tenant_github_repositories as _list_tenant_github_repositories_impl,
    start_github_install as _start_github_install_impl,
)
from orchestrator.api.admin.jira_webhook_response_helpers import (
    build_jira_webhook_diagnostics as _build_jira_webhook_diagnostics_impl,
)
from orchestrator.api.admin.jira_route_service import (
    get_jira_webhook_diagnostics as _get_jira_webhook_diagnostics_route_impl,
    list_jira_projects_for_connection as _list_jira_projects_for_connection_impl,
    run_tenant_jira_webhook_action as _run_tenant_jira_webhook_action_impl,
)
from orchestrator.api.admin.tenant_actions import (
    approve_discord_allowlist_request as _approve_discord_allowlist_request_impl,
    disconnect_tenant_jira as _disconnect_tenant_jira_impl,
    list_discord_allowlist_requests as _list_discord_allowlist_requests_impl,
)
from orchestrator.api.admin.ready_preview import (
    preview_tenant_ready_gate as _preview_tenant_ready_gate_impl,
)
from orchestrator.api.admin.integration_checks import (
    test_github_connection as _test_github_connection_impl,
    test_jira_connection as _test_jira_connection_impl,
)
from orchestrator.api.schemas import (
    DiscordAllowlistApprovalResult,
    DiscordAllowlistRequestRead,
    GitHubRepositoryRead,
    GitHubInstallStart,
    IntegrationTestResult,
    JiraConnectStart,
    JiraWebhookActionResult,
    JiraWebhookDiagnosticsRead,
    JiraProjectRead,
    ReadyGatePreviewRead,
    ReleaseBootstrapReportRead,
    RepoBootstrapStateRead,
)
from orchestrator.core.config import get_settings
from orchestrator.core.platform_secret_service import resolve_platform_secret_ref
from orchestrator.core.tenant_secret_service import resolve_scoped_secret_ref
from orchestrator.core.security import require_admin
from orchestrator.storage.models import JiraOAuthConnection, Project, Tenant
from orchestrator.tools.discord_api import DiscordApiClient
from orchestrator.tools.github_app import github_client_from_tenant_config
from orchestrator.tools.bootstrap import list_repo_bootstrap_states

router = APIRouter(prefix="/api/admin", tags=["admin"])
logger = logging.getLogger(__name__)

JIRA_WEBHOOK_EVENTS = [
    "jira:issue_created",
    "jira:issue_updated",
    "jira:issue_deleted",
    "comment_created",
    "comment_updated",
]
RELEASE_BOOTSTRAP_REQUIRED_STATUSES = ("Ready to Release", "Done")

def _notify_discord_allowlist_approved(
    *,
    session: Session,
    settings,
    tenant_id: str,
    user_id: str,
) -> bool:  # noqa: ANN001
    return _notify_discord_allowlist_approved_core(
        session=session,
        settings=settings,
        tenant_id=tenant_id,
        user_id=user_id,
        resolve_secret_ref_fn=resolve_platform_secret_ref,
        discord_client_factory=DiscordApiClient,
    )
def _jira_oauth_client(
    *,
    session: Session,
    settings,
    tenant_id: str | None = None,
    project_id: str | None = None,
) -> object:  # noqa: ANN401
    return _jira_oauth_client_impl(
        session=session,
        settings=settings,
        tenant_id=tenant_id,
        project_id=project_id,
    )


def _refresh_jira_connection_tokens(
    session: Session,
    *,
    connection: JiraOAuthConnection,
    settings,
    tenant_id: str | None = None,
) -> str:
    return _refresh_jira_connection_tokens_impl(
        session,
        connection=connection,
        settings=settings,
        tenant_id=tenant_id,
    )


def _project_for_tenant_or_404(*, session: Session, tenant_id: str, project_id: str) -> Project:
    project = session.get(Project, project_id)
    if project is None or project.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return project


def _provision_jira_webhook(
    *,
    session: Session,
    tenant: Tenant,
    settings,  # noqa: ANN001
    replace_existing: bool,
) -> JiraWebhookActionResult:
    return _provision_jira_webhook_core(
        session=session,
        tenant=tenant,
        settings=settings,
        replace_existing=replace_existing,
        jira_webhook_events=JIRA_WEBHOOK_EVENTS,
        delete_jira_webhooks_fn=_delete_jira_webhooks,
        parse_managed_webhook_ids_fn=_parse_managed_webhook_ids,
        refresh_jira_connection_tokens_fn=_refresh_jira_connection_tokens,
        jira_oauth_client_fn=_jira_oauth_client,
        jira_webhook_callback_url_fn=_jira_webhook_callback_url,
        jira_webhook_filter_jql_fn=_jira_webhook_filter_jql,
        is_jira_webhook_limit_error_fn=_is_jira_webhook_limit_error,
        cleanup_unmanaged_jira_webhooks_for_connection_fn=_cleanup_unmanaged_jira_webhooks_for_connection,
        parse_jira_webhook_id_fn=_parse_jira_webhook_id,
        remove_managed_webhook_id_from_tenants_fn=_remove_managed_webhook_id_from_tenants,
        is_jira_webhook_single_url_error_fn=_is_jira_webhook_single_url_error,
        extract_jira_webhook_conflict_url_fn=_extract_jira_webhook_conflict_url,
        cleanup_conflicting_jira_webhook_url_fn=_cleanup_conflicting_jira_webhook_url,
    )


def _delete_jira_webhooks(
    *,
    session: Session,
    tenant: Tenant,
    settings,  # noqa: ANN001
) -> tuple[bool, str, list[int]]:
    return _delete_jira_webhooks_core(
        session=session,
        tenant=tenant,
        settings=settings,
        parse_managed_webhook_ids_fn=_parse_managed_webhook_ids,
        refresh_jira_connection_tokens_fn=_refresh_jira_connection_tokens,
        jira_oauth_client_fn=_jira_oauth_client,
    )


@router.post("/jira/connect/start", response_model=JiraConnectStart)
def start_jira_connect(
    return_to: str = Query(default="wizard", pattern="^(wizard|edit)$"),
    tenant_id: str | None = Query(default=None),
    session: Session = Depends(get_session),
    _: str = Depends(require_admin),
) -> JiraConnectStart:
    return _build_jira_connect_start_impl(
        return_to=return_to,
        tenant_id=tenant_id,
        session=session,
        settings=get_settings(),
        jira_oauth_client_fn=_jira_oauth_client,
    )


@router.get("/jira/connect/callback", include_in_schema=False)
def jira_connect_callback(
    code: str = Query(..., min_length=1),
    state_token: str = Query(..., alias="state"),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    redirect_url = _handle_jira_connect_callback_impl(
        code=code,
        state_token=state_token,
        session=session,
        settings=get_settings(),
        jira_oauth_client_fn=_jira_oauth_client,
        auto_provision_jira_webhook_fn=lambda session, tenant, settings: _provision_jira_webhook(
            session=session,
            tenant=tenant,
            settings=settings,
            replace_existing=True,
        ),
    )
    return RedirectResponse(url=redirect_url, status_code=status.HTTP_302_FOUND)


@router.get("/jira/connections/{connection_id}/projects", response_model=list[JiraProjectRead])
def list_jira_projects_for_connection(
    connection_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> list[JiraProjectRead]:
    return _list_jira_projects_for_connection_impl(
        session=session,
        connection_id=connection_id,
        jira_oauth_connection_model=JiraOAuthConnection,
        settings=get_settings(),
        refresh_jira_connection_tokens_fn=_refresh_jira_connection_tokens,
        jira_oauth_client_fn=_jira_oauth_client,
    )


@router.get("/tenants/{tenant_id}/jira/webhooks/diagnostics", response_model=JiraWebhookDiagnosticsRead)
def get_jira_webhook_diagnostics(
    tenant_id: str,
    within_minutes: int = Query(default=60, ge=1, le=1440),
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> JiraWebhookDiagnosticsRead:
    return _get_jira_webhook_diagnostics_route_impl(
        session=session,
        tenant_id=tenant_id,
        within_minutes=within_minutes,
        tenant_model=Tenant,
        settings=get_settings(),
        build_jira_webhook_diagnostics_fn=_build_jira_webhook_diagnostics_impl,
        jira_webhook_callback_url_fn=_jira_webhook_callback_url,
        parse_managed_webhook_ids_fn=_parse_managed_webhook_ids,
    )


@router.post("/tenants/{tenant_id}/jira/webhooks/provision", response_model=JiraWebhookActionResult)
def provision_tenant_jira_webhook(
    tenant_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> JiraWebhookActionResult:
    return _run_tenant_jira_webhook_action_impl(
        session=session,
        tenant_id=tenant_id,
        tenant_model=Tenant,
        settings=get_settings(),
        provision_jira_webhook_fn=_provision_jira_webhook,
        jira_webhook_action_status_code_fn=_jira_webhook_action_status_code,
        replace_existing=False,
    )


@router.post("/tenants/{tenant_id}/jira/webhooks/reset", response_model=JiraWebhookActionResult)
def reset_tenant_jira_webhook(
    tenant_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> JiraWebhookActionResult:
    return _run_tenant_jira_webhook_action_impl(
        session=session,
        tenant_id=tenant_id,
        tenant_model=Tenant,
        settings=get_settings(),
        provision_jira_webhook_fn=_provision_jira_webhook,
        jira_webhook_action_status_code_fn=_jira_webhook_action_status_code,
        replace_existing=True,
    )


@router.post("/tenants/{tenant_id}/jira/disconnect", response_model=JiraWebhookActionResult)
def disconnect_tenant_jira(
    tenant_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> JiraWebhookActionResult:
    return _disconnect_tenant_jira_impl(
        session=session,
        tenant=session.get(Tenant, tenant_id),
        tenant_id=tenant_id,
        settings=get_settings(),
        delete_jira_webhooks_fn=_delete_jira_webhooks,
    )


@router.get(
    "/tenants/{tenant_id}/projects/{project_id}/discord/allowlist-requests",
    response_model=list[DiscordAllowlistRequestRead],
)
def list_discord_allowlist_requests(
    tenant_id: str,
    project_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> list[DiscordAllowlistRequestRead]:
    return _list_discord_allowlist_requests_impl(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        parse_discord_allowlist_requests_fn=_parse_discord_allowlist_requests,
    )


@router.post(
    "/tenants/{tenant_id}/projects/{project_id}/discord/allowlist-requests/{user_id}/approve",
    response_model=DiscordAllowlistApprovalResult,
)
def approve_discord_allowlist_request(
    tenant_id: str,
    project_id: str,
    user_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> DiscordAllowlistApprovalResult:
    return _approve_discord_allowlist_request_impl(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        user_id=user_id,
        settings=get_settings(),
        parse_discord_allowlist_requests_fn=_parse_discord_allowlist_requests,
        notify_discord_allowlist_approved_fn=_notify_discord_allowlist_approved,
    )


@router.get("/tenants/{tenant_id}/ready-preview", response_model=ReadyGatePreviewRead)
def preview_tenant_ready_gate(
    tenant_id: str,
    max_results: int = Query(default=10, ge=1, le=50),
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> ReadyGatePreviewRead:
    return _preview_tenant_ready_gate_impl(
        session=session,
        tenant_id=tenant_id,
        max_results=max_results,
        settings=get_settings(),
        default_ready_jql_fn=_default_ready_jql,
        refresh_jira_connection_tokens_fn=_refresh_jira_connection_tokens,
        jira_oauth_client_fn=_jira_oauth_client,
    )


@router.post("/tenants/{tenant_id}/test-jira", response_model=IntegrationTestResult)
def test_jira_connection(
    tenant_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> IntegrationTestResult:
    return _test_jira_connection_impl(
        session=session,
        tenant_id=tenant_id,
        settings=get_settings(),
        refresh_jira_connection_tokens_fn=_refresh_jira_connection_tokens,
        jira_oauth_client_fn=_jira_oauth_client,
    )


@router.post("/tenants/{tenant_id}/test-github", response_model=IntegrationTestResult)
def test_github_connection(
    tenant_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> IntegrationTestResult:
    return _test_github_connection_impl(
        session=session,
        tenant_id=tenant_id,
        settings=get_settings(),
        with_managed_github_refs_fn=_with_managed_github_refs,
        resolve_scoped_secret_ref_fn=resolve_scoped_secret_ref,
        resolve_platform_secret_ref_fn=resolve_platform_secret_ref,
        github_client_from_tenant_config_fn=github_client_from_tenant_config,
    )

@router.get("/tenants/{tenant_id}/repo-bootstrap", response_model=list[RepoBootstrapStateRead])
def list_tenant_repo_bootstrap_states(
    tenant_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> list[RepoBootstrapStateRead]:
    return _list_tenant_repo_bootstrap_states_impl(
        session=session,
        tenant_id=tenant_id,
        tenant_model=Tenant,
        list_repo_bootstrap_states_fn=list_repo_bootstrap_states,
    )


def _release_bootstrap_report_from_config(*, tenant_id: str, jira_config: dict) -> ReleaseBootstrapReportRead | None:
    return _release_bootstrap_report_from_config_impl(
        tenant_id=tenant_id,
        jira_config=jira_config,
    )


@router.get("/tenants/{tenant_id}/release/bootstrap", response_model=ReleaseBootstrapReportRead | None)
def get_release_bootstrap_report(
    tenant_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> ReleaseBootstrapReportRead | None:
    return _get_release_bootstrap_report_impl(
        session=session,
        tenant_id=tenant_id,
        tenant_model=Tenant,
        release_bootstrap_report_from_config_fn=_release_bootstrap_report_from_config,
    )


@router.post("/tenants/{tenant_id}/release/bootstrap", response_model=ReleaseBootstrapReportRead)
def run_release_bootstrap(
    tenant_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> ReleaseBootstrapReportRead:
    return _run_release_bootstrap_impl(
        session=session,
        tenant_id=tenant_id,
        tenant_model=Tenant,
        settings=get_settings(),
        required_statuses=RELEASE_BOOTSTRAP_REQUIRED_STATUSES,
        compute_release_bootstrap_result_fn=_compute_release_bootstrap_result_impl,
        refresh_jira_connection_tokens_fn=_refresh_jira_connection_tokens,
        jira_oauth_client_fn=_jira_oauth_client,
    )


@router.post("/tenants/{tenant_id}/github/install/start", response_model=GitHubInstallStart)
def start_github_install(
    tenant_id: str,
    return_to: str = Query(default="edit", pattern="^(edit|wizard)$"),
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> GitHubInstallStart:
    return _start_github_install_impl(
        tenant=session.get(Tenant, tenant_id),
        tenant_id=tenant_id,
        return_to=return_to,
        session=session,
        settings=get_settings(),
        resolve_platform_secret_ref_fn=lambda db_session, secret_ref, encryption_key: resolve_platform_secret_ref(
            db_session,
            secret_ref=secret_ref,
            encryption_key=encryption_key,
        ),
    )


@router.get("/github/install/callback", include_in_schema=False)
def github_install_callback(
    state_token: str = Query(..., alias="state"),
    installation_id: str = Query(..., min_length=1),
    setup_action: str | None = Query(default=None),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    redirect_url = _github_install_callback_impl(
        state_token=state_token,
        installation_id=installation_id,
        setup_action=setup_action,
        session=session,
        settings=get_settings(),
    )
    return RedirectResponse(url=redirect_url, status_code=status.HTTP_302_FOUND)


@router.get("/tenants/{tenant_id}/github/repositories", response_model=list[GitHubRepositoryRead])
def list_tenant_github_repositories(
    tenant_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> list[GitHubRepositoryRead]:
    return _list_tenant_github_repositories_impl(
        tenant=session.get(Tenant, tenant_id),
        tenant_id=tenant_id,
        session=session,
        settings=get_settings(),
        with_managed_github_refs_fn=_with_managed_github_refs,
        resolve_scoped_secret_ref_fn=resolve_scoped_secret_ref,
        resolve_platform_secret_ref_fn=resolve_platform_secret_ref,
        github_client_from_tenant_config_fn=github_client_from_tenant_config,
    )
