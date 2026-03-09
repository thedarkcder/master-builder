from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy.orm import Session

from orchestrator.api.dependencies import get_session
from orchestrator.api.admin.schema_mappers import (
    project_to_schema as _project_to_schema,
    run_to_schema as _run_to_schema,
    tenant_to_schema as _tenant_to_schema,
)
from orchestrator.api.admin.project_service import AdminProjectService
from orchestrator.api.admin.config_helpers import (
    validate_codex_assets_for_tenant_init as _validate_codex_assets_for_tenant_init_core,
)
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
from orchestrator.api.admin.tenant_project_routes_service import (
    create_project as _create_project_route_impl,
    create_tenant as _create_tenant_route_impl,
    delete_tenant as _delete_tenant_route_impl,
    get_project as _get_project_route_impl,
    get_tenant as _get_tenant_route_impl,
    list_projects as _list_projects_route_impl,
    list_tenants as _list_tenants_route_impl,
    set_tenant_archive_state as _set_tenant_archive_state_route_impl,
    update_project as _update_project_route_impl,
    update_tenant as _update_tenant_route_impl,
)
from orchestrator.api.admin.route_helpers import (
    allocate_tenant_id as _allocate_tenant_id,
    cleanup_conflicting_jira_webhook_url as _cleanup_conflicting_jira_webhook_url,
    cleanup_unmanaged_jira_webhooks_for_connection as _cleanup_unmanaged_jira_webhooks_for_connection,
    discover_project_run_board_id as _discover_project_run_board_id_impl,
    ensure_default_project_for_tenant as _ensure_default_project_for_tenant,
    ensure_project_repository_checkout as _ensure_project_repository_checkout,
    jira_oauth_client as _jira_oauth_client_impl,
    jira_webhook_action_status_code as _jira_webhook_action_status_code,
    parse_discord_allowlist_requests as _parse_discord_allowlist_requests,
    refresh_jira_connection_tokens as _refresh_jira_connection_tokens_impl,
    remove_managed_webhook_id_from_tenants as _remove_managed_webhook_id_from_tenants,
    sync_tenant_jira_project_keys as _sync_tenant_jira_project_keys,
    with_managed_github_refs as _with_managed_github_refs,
    with_preserved_jira_system_fields as _with_preserved_jira_system_fields,
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
from orchestrator.api.admin.runs_query import build_runs_query as _build_runs_query_impl
from orchestrator.api.admin.runs_service import (
    cancel_run_admin as _cancel_run_admin_impl,
    get_run as _get_run_impl,
    list_run_log_events as _list_run_log_events_impl,
    list_run_events as _list_run_events_impl,
    list_runs as _list_runs_impl,
    rerun_run as _rerun_run_impl,
)
from orchestrator.api.admin.agent_activity_service import (
    list_agent_activity as _list_agent_activity_impl,
)
from orchestrator.api.admin.run_event_stream_service import (
    stream_run_events_ndjson as _stream_run_events_ndjson_impl,
)
from orchestrator.api.admin.codex_logs_service import (
    list_codex_log_events as _list_codex_log_events_impl,
    stream_codex_events_ndjson as _stream_codex_events_ndjson_impl,
)
from orchestrator.api.admin.project_metrics_service import (
    project_execution_metrics as _project_execution_metrics_impl,
)
from orchestrator.api.admin.alert_policy_service import (
    evaluate_alerts as _evaluate_alerts_impl,
)
from orchestrator.api.admin.tenant_health_service import (
    tenant_health as _tenant_health_impl,
)
from orchestrator.api.admin.observability_service import (
    platform_observability as _platform_observability_impl,
    tenant_observability as _tenant_observability_impl,
    project_observability as _project_observability_impl,
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
from orchestrator.api.admin.tenant_crud import (
    create_tenant as _create_tenant_impl,
    delete_tenant as _delete_tenant_impl,
    get_tenant_or_404 as _get_tenant_or_404_impl,
    set_tenant_archive_state as _set_tenant_archive_state_impl,
    update_tenant as _update_tenant_impl,
)
from orchestrator.api.admin.integration_checks import (
    test_github_connection as _test_github_connection_impl,
    test_jira_connection as _test_jira_connection_impl,
)
from orchestrator.api.admin.project_normalization import (
    normalize_project_discord_config as _normalize_project_discord_config,
    normalize_project_key as _normalize_project_key,
    normalize_project_repo as _normalize_project_repo,
    normalize_string_map as _normalize_string_map,
    resolve_project_discord_channel_name as _resolve_project_discord_channel_name,  # noqa: F401
    with_preserved_discord_system_fields as _with_preserved_discord_system_fields,
)
from orchestrator.api.admin.tenant_project_helpers import (
    resolve_project_discord_channel_binding as _resolve_project_discord_channel_binding_impl,
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
    ReadyGatePreviewRead,
    JiraProjectRead,
    PlatformObservabilityRead,
    ProjectObservabilityRead,
    ProjectCreate,
    ProjectRead,
    TenantObservabilityRead,
    ProjectUpdate,
    ReleaseBootstrapReportRead,
    RepoBootstrapStateRead,
    RunRead,
    RunEventRead,
    RunLogEventRead,
    AgentActivityRead,
    ProjectExecutionMetricsRead,
    AlertEvaluationRead,
    TenantHealthRead,
    TenantCreate,
    TenantRead,
    TenantUpdate,
)
from orchestrator.core.config import get_settings
from orchestrator.core.project_policy import normalize_project_policy_overrides
from orchestrator.core.jira_links import tenant_jira_issue_url
from orchestrator.core.worker.run_lifecycle import resolve_project_for_run as _resolve_project_for_run
from orchestrator.core.platform_secret_service import resolve_platform_secret_ref
from orchestrator.core.tenant_secret_service import resolve_scoped_secret_ref
from orchestrator.core.security import require_admin
from orchestrator.storage.models import JiraOAuthConnection, Project, Run, Tenant
from orchestrator.tools.discord_api import DiscordApiClient
from orchestrator.tools.github_app import github_client_from_tenant_config
from orchestrator.tools.bootstrap import list_repo_bootstrap_states

router = APIRouter(prefix="/api/admin", tags=["admin"])
logger = logging.getLogger(__name__)

try:
    import psycopg
except ImportError:  # pragma: no cover - dependency is required at runtime
    psycopg = None

JIRA_WEBHOOK_EVENTS = [
    "jira:issue_created",
    "jira:issue_updated",
    "jira:issue_deleted",
    "comment_created",
    "comment_updated",
]
RELEASE_BOOTSTRAP_REQUIRED_STATUSES = ("Ready to Release", "Done")


def _validate_codex_assets_for_tenant_init() -> None:
    _validate_codex_assets_for_tenant_init_core(
        settings=get_settings(),
        module_file=__file__,
    )


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


def _resolve_project_discord_channel_binding(
    *,
    session: Session,
    settings,  # noqa: ANN001
    tenant: Tenant,
    project,
    discord_config: dict,
) -> dict:
    return _resolve_project_discord_channel_binding_impl(
        session=session,
        settings=settings,
        tenant=tenant,
        project=project,
        discord_config=discord_config,
        resolve_project_discord_channel_name_fn=_resolve_project_discord_channel_name,
    )


def _admin_project_service() -> AdminProjectService:
    def _resolve_project_run_board_id(
        *,
        session: Session,
        tenant: Tenant,
        jira_project_key: str,
        settings,  # noqa: ANN001
    ) -> int | None:
        return _discover_project_run_board_id_impl(
            session=session,
            tenant=tenant,
            jira_project_key=jira_project_key,
            settings=settings,
        )

    return AdminProjectService(
        normalize_project_repo=_normalize_project_repo,
        normalize_project_key=_normalize_project_key,
        normalize_project_policy_overrides=normalize_project_policy_overrides,
        normalize_string_map=_normalize_string_map,
        normalize_project_discord_config=_normalize_project_discord_config,
        with_preserved_discord_system_fields=_with_preserved_discord_system_fields,
        resolve_project_discord_channel_binding=_resolve_project_discord_channel_binding,
        sync_tenant_jira_project_keys=_sync_tenant_jira_project_keys,
        ensure_project_repository_checkout=_ensure_project_repository_checkout,
        resolve_project_run_board_id=_resolve_project_run_board_id,
        project_to_schema=_project_to_schema,
        settings_factory=get_settings,
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


@router.get("/tenants", response_model=list[TenantRead])
def list_tenants(
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> list[TenantRead]:
    return _list_tenants_route_impl(
        session=session,
        tenant_model=Tenant,
        tenant_to_schema_fn=_tenant_to_schema,
    )


@router.post("/tenants", response_model=TenantRead, status_code=status.HTTP_201_CREATED)
def create_tenant(
    payload: TenantCreate,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> TenantRead:
    return _create_tenant_route_impl(
        session=session,
        payload=payload,
        validate_codex_assets_for_tenant_init_fn=_validate_codex_assets_for_tenant_init,
        create_tenant_fn=_create_tenant_impl,
        allocate_tenant_id_fn=_allocate_tenant_id,
        with_preserved_jira_system_fields_fn=_with_preserved_jira_system_fields,
        with_managed_github_refs_fn=_with_managed_github_refs,
        with_preserved_discord_system_fields_fn=_with_preserved_discord_system_fields,
        ensure_default_project_for_tenant_fn=_ensure_default_project_for_tenant,
        sync_tenant_jira_project_keys_fn=_sync_tenant_jira_project_keys,
        tenant_to_schema_fn=_tenant_to_schema,
    )


@router.get("/tenants/{tenant_id}", response_model=TenantRead)
def get_tenant(
    tenant_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> TenantRead:
    return _get_tenant_route_impl(
        session=session,
        tenant_id=tenant_id,
        get_tenant_or_404_fn=_get_tenant_or_404_impl,
        tenant_to_schema_fn=_tenant_to_schema,
    )


@router.put("/tenants/{tenant_id}", response_model=TenantRead)
def update_tenant(
    tenant_id: str,
    payload: TenantUpdate,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> TenantRead:
    return _update_tenant_route_impl(
        session=session,
        tenant_id=tenant_id,
        payload=payload,
        validate_codex_assets_for_tenant_init_fn=_validate_codex_assets_for_tenant_init,
        update_tenant_fn=_update_tenant_impl,
        with_preserved_jira_system_fields_fn=_with_preserved_jira_system_fields,
        with_managed_github_refs_fn=_with_managed_github_refs,
        with_preserved_discord_system_fields_fn=_with_preserved_discord_system_fields,
        ensure_default_project_for_tenant_fn=_ensure_default_project_for_tenant,
        sync_tenant_jira_project_keys_fn=_sync_tenant_jira_project_keys,
        tenant_to_schema_fn=_tenant_to_schema,
    )


@router.delete("/tenants/{tenant_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_tenant(
    tenant_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> Response:
    return _delete_tenant_route_impl(
        session=session,
        tenant_id=tenant_id,
        delete_tenant_fn=_delete_tenant_impl,
    )


@router.post("/tenants/{tenant_id}/archive", response_model=TenantRead)
def archive_tenant(
    tenant_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> TenantRead:
    return _set_tenant_archive_state_route_impl(
        session=session,
        tenant_id=tenant_id,
        is_enabled=False,
        set_tenant_archive_state_fn=_set_tenant_archive_state_impl,
        tenant_to_schema_fn=_tenant_to_schema,
    )


@router.post("/tenants/{tenant_id}/unarchive", response_model=TenantRead)
def unarchive_tenant(
    tenant_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> TenantRead:
    return _set_tenant_archive_state_route_impl(
        session=session,
        tenant_id=tenant_id,
        is_enabled=True,
        set_tenant_archive_state_fn=_set_tenant_archive_state_impl,
        tenant_to_schema_fn=_tenant_to_schema,
    )


@router.get("/tenants/{tenant_id}/projects", response_model=list[ProjectRead])
def list_projects(
    tenant_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> list[ProjectRead]:
    return _list_projects_route_impl(
        session=session,
        tenant_id=tenant_id,
        admin_project_service_factory=_admin_project_service,
    )  # type: ignore[return-value]


@router.post("/tenants/{tenant_id}/projects", response_model=ProjectRead, status_code=status.HTTP_201_CREATED)
def create_project(
    tenant_id: str,
    payload: ProjectCreate,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> ProjectRead:
    return _create_project_route_impl(
        session=session,
        tenant_id=tenant_id,
        payload=payload,
        admin_project_service_factory=_admin_project_service,
    )  # type: ignore[return-value]


@router.get("/tenants/{tenant_id}/projects/{project_id}", response_model=ProjectRead)
def get_project(
    tenant_id: str,
    project_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> ProjectRead:
    return _get_project_route_impl(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        admin_project_service_factory=_admin_project_service,
    )  # type: ignore[return-value]


@router.put("/tenants/{tenant_id}/projects/{project_id}", response_model=ProjectRead)
def update_project(
    tenant_id: str,
    project_id: str,
    payload: ProjectUpdate,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> ProjectRead:
    return _update_project_route_impl(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        payload=payload,
        admin_project_service_factory=_admin_project_service,
    )  # type: ignore[return-value]


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


@router.get("/runs", response_model=list[RunRead])
def list_runs(
    tenant_id: str | None = Query(default=None),
    project_id: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    issue_query: str | None = Query(default=None, alias="issue"),
    pr_state: str | None = Query(default=None, alias="pr_state"),
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> list[RunRead]:
    return _list_runs_impl(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        status_filter=status_filter,
        issue_query=issue_query,
        pr_state=pr_state,
        from_time=from_time,
        to_time=to_time,
        limit=limit,
        offset=offset,
        build_runs_query_fn=_build_runs_query_impl,
        run_to_schema_fn=_run_to_schema,
        tenant_model=Tenant,
        tenant_jira_issue_url_fn=tenant_jira_issue_url,
    )


@router.get("/runs/{run_id}", response_model=RunRead)
def get_run(
    run_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> RunRead:
    return _get_run_impl(
        session=session,
        run_id=run_id,
        run_model=Run,
        run_to_schema_fn=_run_to_schema,
        tenant_model=Tenant,
        tenant_jira_issue_url_fn=tenant_jira_issue_url,
    )


@router.post("/runs/{run_id}/rerun", response_model=RunRead, status_code=status.HTTP_201_CREATED)
def rerun_failed_run(
    run_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> RunRead:
    return _rerun_run_impl(
        session=session,
        run_id=run_id,
        run_model=Run,
        tenant_model=Tenant,
        resolve_project_for_run_fn=_resolve_project_for_run,
        run_to_schema_fn=_run_to_schema,
    )


@router.post("/runs/{run_id}/cancel", response_model=RunRead)
def cancel_run(
    run_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> RunRead:
    return _cancel_run_admin_impl(
        session=session,
        run_id=run_id,
        run_to_schema_fn=_run_to_schema,
        cancelled_by="admin",
    )


@router.get("/runs/{run_id}/events", response_model=list[RunEventRead])
def list_run_events(
    run_id: str,
    limit: int = Query(default=200, ge=1, le=500),
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> list[RunEventRead]:
    return _list_run_events_impl(
        session=session,
        run_id=run_id,
        run_model=Run,
        run_event_schema_cls=RunEventRead,
        limit=limit,
    )


@router.get("/runs/{run_id}/logs", response_model=list[RunLogEventRead])
def list_run_logs(
    run_id: str,
    limit: int = Query(default=200, ge=1, le=1000),
    before_recorded_at: datetime | None = Query(default=None),
    before_event_id: str | None = Query(default=None),
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> list[RunLogEventRead]:
    return _list_run_log_events_impl(
        session=session,
        run_id=run_id,
        run_model=Run,
        run_log_schema_cls=RunLogEventRead,
        limit=limit,
        before_recorded_at=before_recorded_at,
        before_event_id=before_event_id,
    )


@router.get("/runs/{run_id}/events/stream")
def stream_run_events(
    run_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> StreamingResponse:
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    return StreamingResponse(
        _stream_run_events_ndjson_impl(
            session=session,
            run_id=run_id,
            run_model=Run,
            settings=get_settings(),
            psycopg_module=psycopg,
        ),
        media_type="application/x-ndjson",
    )


@router.get("/codex/logs", response_model=list[RunLogEventRead])
def list_codex_logs(
    tenant_id: str | None = Query(default=None),
    project_id: str | None = Query(default=None),
    run_id: str | None = Query(default=None),
    channel: str | None = Query(default=None),
    command: str | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=2000),
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> list[RunLogEventRead]:
    return _list_codex_log_events_impl(
        session=session,
        run_log_schema_cls=RunLogEventRead,
        tenant_id=tenant_id,
        project_id=project_id,
        run_id=run_id,
        channel=channel,
        command=command,
        limit=limit,
    )


@router.get("/codex/events/stream")
def stream_codex_events(
    tenant_id: str | None = Query(default=None),
    project_id: str | None = Query(default=None),
    run_id: str | None = Query(default=None),
    channel: str | None = Query(default=None),
    command: str | None = Query(default=None),
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
    ) -> StreamingResponse:
    return StreamingResponse(
        _stream_codex_events_ndjson_impl(
            session=session,
            settings=get_settings(),
            psycopg_module=psycopg,
            tenant_id=tenant_id,
            project_id=project_id,
            run_id=run_id,
            channel=channel,
            command=command,
        ),
        media_type="application/x-ndjson",
    )


@router.get("/agents/activity", response_model=list[AgentActivityRead])
def list_agent_activity(
    tenant_id: str | None = Query(default=None),
    project_id: str | None = Query(default=None),
    heartbeat_timeout_seconds: int = Query(default=300, ge=1, le=86400),
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> list[AgentActivityRead]:
    return _list_agent_activity_impl(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        heartbeat_timeout_seconds=heartbeat_timeout_seconds,
    )


@router.get("/tenants/{tenant_id}/projects/{project_id}/metrics", response_model=ProjectExecutionMetricsRead)
def get_project_execution_metrics(
    tenant_id: str,
    project_id: str,
    sla_seconds: int = Query(default=1800, ge=1, le=86400),
    stale_queue_seconds: int = Query(default=7200, ge=1, le=604800),
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> ProjectExecutionMetricsRead:
    return _project_execution_metrics_impl(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        sla_seconds=sla_seconds,
        stale_queue_seconds=stale_queue_seconds,
    )


@router.get("/alerts/evaluate", response_model=AlertEvaluationRead)
def evaluate_alerts(
    tenant_id: str | None = Query(default=None),
    cooldown_seconds: int = Query(default=600, ge=1, le=3600),
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> AlertEvaluationRead:
    return _evaluate_alerts_impl(
        session=session,
        tenant_id=tenant_id,
        cooldown_seconds=cooldown_seconds,
    )


@router.get("/tenants/{tenant_id}/health", response_model=TenantHealthRead)
def get_tenant_health(
    tenant_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> TenantHealthRead:
    return _tenant_health_impl(
        session=session,
        tenant_id=tenant_id,
    )


@router.get("/observability/platform", response_model=PlatformObservabilityRead)
def platform_observability(
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> PlatformObservabilityRead:
    return _platform_observability_impl(session=session)


@router.get("/observability/tenants/{tenant_id}", response_model=TenantObservabilityRead)
def tenant_observability(
    tenant_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> TenantObservabilityRead:
    return _tenant_observability_impl(
        session=session,
        tenant_id=tenant_id,
    )


@router.get(
    "/observability/tenants/{tenant_id}/projects/{project_id}",
    response_model=ProjectObservabilityRead,
)
def project_observability(
    tenant_id: str,
    project_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> ProjectObservabilityRead:
    return _project_observability_impl(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
    )
