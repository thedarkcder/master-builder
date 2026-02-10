from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.api.dependencies import get_session
from orchestrator.api.admin_schema_mappers import (
    project_to_schema as _project_to_schema,
    run_to_schema as _run_to_schema,
    tenant_to_schema as _tenant_to_schema,
)
from orchestrator.api.admin_project_service import AdminProjectService
from orchestrator.api.admin_jira_webhook_helpers import (
    default_ready_jql as _default_ready_jql,
    extract_jira_webhook_conflict_url as _extract_jira_webhook_conflict_url,
    is_jira_webhook_limit_error as _is_jira_webhook_limit_error,
    is_jira_webhook_single_url_error as _is_jira_webhook_single_url_error,
    jira_webhook_callback_url as _jira_webhook_callback_url,
    jira_webhook_filter_jql as _jira_webhook_filter_jql,
    parse_jira_webhook_id as _parse_jira_webhook_id,
    parse_managed_webhook_ids as _parse_managed_webhook_ids,
)
from orchestrator.api.admin_jira_webhook_cleanup import (
    all_managed_webhook_ids as _all_managed_webhook_ids_impl,
    cleanup_conflicting_jira_webhook_url as _cleanup_conflicting_jira_webhook_url_impl,
    cleanup_unmanaged_jira_webhooks_for_connection as _cleanup_unmanaged_jira_webhooks_for_connection_impl,
    remove_managed_webhook_id_from_tenants as _remove_managed_webhook_id_from_tenants_impl,
)
from orchestrator.api.admin_jira_oauth_helpers import (
    jira_oauth_client as _jira_oauth_client_impl,
    refresh_jira_connection_tokens as _refresh_jira_connection_tokens_impl,
    resolve_secret_ref as _resolve_secret_ref_impl,
)
from orchestrator.api.admin_discord_allowlist_helpers import (
    notify_discord_allowlist_approved as _notify_discord_allowlist_approved_impl,
    parse_discord_allowlist_requests as _parse_discord_allowlist_requests_impl,
)
from orchestrator.api.admin_jira_webhook_delete import (
    delete_jira_webhooks as _delete_jira_webhooks_impl,
)
from orchestrator.api.admin_config_helpers import (
    validate_codex_assets_for_tenant_init as _validate_codex_assets_for_tenant_init_impl,
    with_managed_github_refs as _with_managed_github_refs_impl,
    with_preserved_jira_system_fields as _with_preserved_jira_system_fields_impl,
)
from orchestrator.api.admin_release_bootstrap_helpers import (
    compute_release_bootstrap_result as _compute_release_bootstrap_result_impl,
    release_bootstrap_report_from_config as _release_bootstrap_report_from_config_impl,
)
from orchestrator.api.admin_jira_connect_flow import (
    build_jira_connect_start as _build_jira_connect_start_impl,
    handle_jira_connect_callback as _handle_jira_connect_callback_impl,
)
from orchestrator.api.admin_github_helpers import (
    github_install_callback as _github_install_callback_impl,
    list_tenant_github_repositories as _list_tenant_github_repositories_impl,
    start_github_install as _start_github_install_impl,
)
from orchestrator.api.admin_runs_query import build_runs_query as _build_runs_query_impl
from orchestrator.api.admin_jira_webhook_response_helpers import (
    build_jira_webhook_diagnostics as _build_jira_webhook_diagnostics_impl,
    jira_webhook_action_status_code as _jira_webhook_action_status_code_impl,
)
from orchestrator.api.admin_tenant_actions import (
    approve_discord_allowlist_request as _approve_discord_allowlist_request_impl,
    disconnect_tenant_jira as _disconnect_tenant_jira_impl,
    list_discord_allowlist_requests as _list_discord_allowlist_requests_impl,
)
from orchestrator.api.admin_ready_preview import (
    preview_tenant_ready_gate as _preview_tenant_ready_gate_impl,
)
from orchestrator.api.admin_tenant_crud import (
    create_tenant as _create_tenant_impl,
    delete_tenant as _delete_tenant_impl,
    get_tenant_or_404 as _get_tenant_or_404_impl,
    set_tenant_archive_state as _set_tenant_archive_state_impl,
    update_tenant as _update_tenant_impl,
)
from orchestrator.api.admin_project_normalization import (
    default_project_name_from_repo as _default_project_name_from_repo,
    normalize_project_discord_config as _normalize_project_discord_config,
    normalize_project_key as _normalize_project_key,
    normalize_project_repo as _normalize_project_repo,
    normalize_string_map as _normalize_string_map,
    resolve_project_discord_channel_name as _resolve_project_discord_channel_name,
    sanitize_discord_channel_name as _sanitize_discord_channel_name,
    with_preserved_discord_system_fields as _with_preserved_discord_system_fields,
)
from orchestrator.api.admin_tenant_project_helpers import (
    allocate_tenant_id as _allocate_tenant_id_impl,
    ensure_default_project_for_tenant as _ensure_default_project_for_tenant_impl,
    primary_jira_project_key as _primary_jira_project_key_impl,
    primary_repo_url as _primary_repo_url_impl,
    resolve_project_discord_channel_binding as _resolve_project_discord_channel_binding_impl,
    slugify_tenant_name as _slugify_tenant_name_impl,
    sync_tenant_jira_project_keys as _sync_tenant_jira_project_keys_impl,
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
    ProjectCreate,
    ProjectRead,
    ProjectUpdate,
    ReleaseBootstrapReportRead,
    RepoBootstrapStateRead,
    RunRead,
    TenantCreate,
    TenantRead,
    TenantUpdate,
)
from orchestrator.core.config import get_settings
from orchestrator.core.enforcement_context import EnforcementAssetsError, validate_enforcement_assets
from orchestrator.core.project_policy import normalize_project_policy_overrides
from orchestrator.core.secret_manager import (
    resolve_scoped_secret_ref,
)
from orchestrator.core.security import require_admin
from orchestrator.storage.models import JiraOAuthConnection, Project, Run, Tenant
from orchestrator.tools.github_app import github_client_from_tenant_config
from orchestrator.tools.discord_api import DiscordApiClient
from orchestrator.tools.jira_oauth import JiraOAuthClient, JiraOAuthError
from orchestrator.tools.bootstrap import list_repo_bootstrap_states

router = APIRouter(prefix="/api/admin", tags=["admin"])

JIRA_WEBHOOK_EVENTS = [
    "jira:issue_created",
    "jira:issue_updated",
    "jira:issue_deleted",
    "comment_created",
    "comment_updated",
]
RELEASE_BOOTSTRAP_REQUIRED_STATUSES = ("Ready to Release", "Done")


def _slugify_tenant_name(name: str) -> str:
    return _slugify_tenant_name_impl(name)


def _allocate_tenant_id(session: Session, *, name: str) -> str:
    return _allocate_tenant_id_impl(session, name=name)


def _primary_jira_project_key(jira_config: dict) -> str | None:
    return _primary_jira_project_key_impl(jira_config)


def _primary_repo_url(repos_config: dict) -> str | None:
    return _primary_repo_url_impl(repos_config)


def _resolve_project_discord_channel_binding(
    *,
    session: Session,
    settings,  # noqa: ANN001
    tenant: Tenant,
    project: Project,
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


def _ensure_default_project_for_tenant(session: Session, *, tenant: Tenant) -> None:
    _ensure_default_project_for_tenant_impl(
        session,
        tenant=tenant,
        default_project_name_from_repo_fn=_default_project_name_from_repo,
    )


def _sync_tenant_jira_project_keys(session: Session, *, tenant: Tenant) -> None:
    _sync_tenant_jira_project_keys_impl(
        session,
        tenant=tenant,
        normalize_project_key_fn=_normalize_project_key,
    )


def _with_managed_github_refs(raw_github_config: dict) -> dict:
    return _with_managed_github_refs_impl(
        raw_github_config=raw_github_config,
        settings=get_settings(),
    )


def _validate_codex_assets_for_tenant_init() -> None:
    _validate_codex_assets_for_tenant_init_impl(
        settings=get_settings(),
        module_file=__file__,
        validate_enforcement_assets_fn=validate_enforcement_assets,
    )


def _resolve_secret_ref(
    session: Session,
    *,
    ref_name: str,
    settings,
    tenant_id: str | None = None,
    project_id: str | None = None,
) -> str:  # noqa: ANN001
    return _resolve_secret_ref_impl(
        session,
        ref_name=ref_name,
        settings=settings,
        tenant_id=tenant_id,
        project_id=project_id,
    )


def _jira_oauth_client(
    *,
    session: Session,
    settings,
    tenant_id: str | None = None,
    project_id: str | None = None,
) -> JiraOAuthClient:  # noqa: ANN001
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
) -> str:  # noqa: ANN001
    return _refresh_jira_connection_tokens_impl(
        session,
        connection=connection,
        settings=settings,
        tenant_id=tenant_id,
        jira_oauth_client_fn=_jira_oauth_client,
    )


def _all_managed_webhook_ids(session: Session) -> set[int]:
    return _all_managed_webhook_ids_impl(
        session=session,
        parse_managed_webhook_ids_fn=_parse_managed_webhook_ids,
    )


def _cleanup_unmanaged_jira_webhooks_for_connection(
    *,
    session: Session,
    client: JiraOAuthClient,
    access_token: str,
    cloud_id: str,
) -> tuple[int, str]:
    return _cleanup_unmanaged_jira_webhooks_for_connection_impl(
        session=session,
        client=client,
        access_token=access_token,
        cloud_id=cloud_id,
        parse_managed_webhook_ids_fn=_parse_managed_webhook_ids,
        parse_jira_webhook_id_fn=_parse_jira_webhook_id,
    )


def _cleanup_conflicting_jira_webhook_url(
    *,
    session: Session,
    client: JiraOAuthClient,
    access_token: str,
    cloud_id: str,
    callback_url: str,
    conflicting_url: str | None,
) -> tuple[int, str]:
    return _cleanup_conflicting_jira_webhook_url_impl(
        session=session,
        client=client,
        access_token=access_token,
        cloud_id=cloud_id,
        callback_url=callback_url,
        conflicting_url=conflicting_url,
        parse_jira_webhook_id_fn=_parse_jira_webhook_id,
        remove_managed_webhook_id_from_tenants_fn=_remove_managed_webhook_id_from_tenants,
    )


def _remove_managed_webhook_id_from_tenants(*, session: Session, webhook_id: int) -> int:
    return _remove_managed_webhook_id_from_tenants_impl(
        session=session,
        webhook_id=webhook_id,
        parse_managed_webhook_ids_fn=_parse_managed_webhook_ids,
    )


def _with_preserved_jira_system_fields(*, existing: dict, proposed: dict) -> dict:
    return _with_preserved_jira_system_fields_impl(existing=existing, proposed=proposed)


def _parse_discord_allowlist_requests(
    discord_config: dict | None,
    *,
    project_id: str | None = None,
) -> list[DiscordAllowlistRequestRead]:
    return _parse_discord_allowlist_requests_impl(
        discord_config,
        project_id=project_id,
    )


def _notify_discord_allowlist_approved(
    *,
    session: Session,
    settings,
    tenant_id: str,
    user_id: str,
) -> bool:  # noqa: ANN001
    return _notify_discord_allowlist_approved_impl(
        session=session,
        settings=settings,
        tenant_id=tenant_id,
        user_id=user_id,
        resolve_secret_ref_fn=resolve_scoped_secret_ref,
        discord_client_factory=DiscordApiClient,
    )


def _delete_jira_webhooks(
    *,
    session: Session,
    tenant: Tenant,
    settings,  # noqa: ANN001
) -> tuple[bool, str, list[int]]:
    return _delete_jira_webhooks_impl(
        session=session,
        tenant=tenant,
        settings=settings,
        parse_managed_webhook_ids_fn=_parse_managed_webhook_ids,
        refresh_jira_connection_tokens_fn=_refresh_jira_connection_tokens,
        jira_oauth_client_fn=_jira_oauth_client,
    )


def _admin_project_service() -> AdminProjectService:
    return AdminProjectService(
        normalize_project_repo=_normalize_project_repo,
        normalize_project_key=_normalize_project_key,
        normalize_project_policy_overrides=normalize_project_policy_overrides,
        normalize_string_map=_normalize_string_map,
        normalize_project_discord_config=_normalize_project_discord_config,
        with_preserved_discord_system_fields=_with_preserved_discord_system_fields,
        resolve_project_discord_channel_binding=_resolve_project_discord_channel_binding,
        sync_tenant_jira_project_keys=_sync_tenant_jira_project_keys,
        project_to_schema=_project_to_schema,
        settings_factory=get_settings,
    )


def _provision_jira_webhook(
    *,
    session: Session,
    tenant: Tenant,
    settings,  # noqa: ANN001
    replace_existing: bool,
) -> JiraWebhookActionResult:
    action_name = "reset" if replace_existing else "provision"
    jira_config = dict(tenant.jira_config)
    connection_id = jira_config.get("connection_id")
    if not isinstance(connection_id, str) or not connection_id:
        return JiraWebhookActionResult(
            ok=False,
            action=action_name,
            details="Jira OAuth connection is not linked for this tenant",
            webhook_ids=[],
        )

    connection = session.get(JiraOAuthConnection, connection_id)
    if connection is None:
        return JiraWebhookActionResult(
            ok=False,
            action=action_name,
            details="Configured Jira connection was not found",
            webhook_ids=[],
        )

    if replace_existing:
        delete_ok, delete_details, _ = _delete_jira_webhooks(
            session=session,
            tenant=tenant,
            settings=settings,
        )
        if not delete_ok:
            return JiraWebhookActionResult(
                ok=False,
                action="reset",
                details=delete_details,
                webhook_ids=_parse_managed_webhook_ids(jira_config),
            )
        session.refresh(tenant)
        jira_config = dict(tenant.jira_config)

    access_token = _refresh_jira_connection_tokens(
        session,
        connection=connection,
        settings=settings,
        tenant_id=tenant.tenant_id,
    )
    client = _jira_oauth_client(session=session, settings=settings, tenant_id=tenant.tenant_id)
    callback_url = _jira_webhook_callback_url(settings=settings, tenant_id=tenant.tenant_id)
    jql_filter = _jira_webhook_filter_jql(jira_config)
    webhook_ids: list[int] | None = None
    cleanup_note: str | None = None
    try:
        webhook_ids = client.register_webhook(
            access_token=access_token,
            cloud_id=connection.cloud_id,
            callback_url=callback_url,
            jql_filter=jql_filter,
            events=JIRA_WEBHOOK_EVENTS,
        )
    except (ValueError, JiraOAuthError) as exc:
        if _is_jira_webhook_limit_error(exc):
            try:
                deleted_count, cleanup_details = _cleanup_unmanaged_jira_webhooks_for_connection(
                    session=session,
                    client=client,
                    access_token=access_token,
                    cloud_id=connection.cloud_id,
                )
                cleanup_note = cleanup_details
                if deleted_count == 0:
                    current_tenant_ids = _parse_managed_webhook_ids(jira_config)
                    if current_tenant_ids:
                        client.delete_webhooks(
                            access_token=access_token,
                            cloud_id=connection.cloud_id,
                            webhook_ids=current_tenant_ids,
                        )
                        deleted_count = len(current_tenant_ids)
                        cleanup_note = (
                            f"{cleanup_details} Deleted {deleted_count} existing tenant Jira webhook(s)."
                        )
                if deleted_count == 0:
                    all_webhooks = client.list_webhooks(access_token=access_token, cloud_id=connection.cloud_id)
                    rollover_id: int | None = None
                    for item in all_webhooks:
                        parsed = _parse_jira_webhook_id(item.get("id"))
                        if parsed is not None:
                            rollover_id = parsed
                            break
                    if rollover_id is not None:
                        client.delete_webhooks(
                            access_token=access_token,
                            cloud_id=connection.cloud_id,
                            webhook_ids=[rollover_id],
                        )
                        touched_tenants = _remove_managed_webhook_id_from_tenants(
                            session=session,
                            webhook_id=rollover_id,
                        )
                        deleted_count = 1
                        tenant_note = (
                            f" Removed stale managed reference from {touched_tenants} tenant(s)."
                            if touched_tenants > 0
                            else ""
                        )
                        cleanup_note = (
                            f"{cleanup_details} Deleted 1 rollover Jira webhook ({rollover_id}) to free capacity."
                            f"{tenant_note}"
                        )
                if deleted_count > 0:
                    webhook_ids = client.register_webhook(
                        access_token=access_token,
                        cloud_id=connection.cloud_id,
                        callback_url=callback_url,
                        jql_filter=jql_filter,
                        events=JIRA_WEBHOOK_EVENTS,
                    )
            except (ValueError, JiraOAuthError) as cleanup_exc:
                jira_config["webhook_last_error"] = (
                    "Failed to provision Jira webhook: "
                    f"{exc}. Cleanup attempt failed: {cleanup_exc}"
                )
                tenant.jira_config = jira_config
                tenant.updated_at = datetime.now(timezone.utc)
                session.commit()
                return JiraWebhookActionResult(
                    ok=False,
                    action=action_name,
                    details=jira_config["webhook_last_error"],
                    webhook_ids=_parse_managed_webhook_ids(jira_config),
                )
        if webhook_ids is None:
            if _is_jira_webhook_single_url_error(exc):
                try:
                    conflicting_url = _extract_jira_webhook_conflict_url(exc)
                    deleted_count, cleanup_details = _cleanup_conflicting_jira_webhook_url(
                        session=session,
                        client=client,
                        access_token=access_token,
                        cloud_id=connection.cloud_id,
                        callback_url=callback_url,
                        conflicting_url=conflicting_url,
                    )
                    if deleted_count > 0:
                        cleanup_note = cleanup_details
                        webhook_ids = client.register_webhook(
                            access_token=access_token,
                            cloud_id=connection.cloud_id,
                            callback_url=callback_url,
                            jql_filter=jql_filter,
                            events=JIRA_WEBHOOK_EVENTS,
                        )
                except (ValueError, JiraOAuthError) as cleanup_exc:
                    jira_config["webhook_last_error"] = (
                        "Failed to provision Jira webhook: "
                        f"{exc}. URL-conflict cleanup failed: {cleanup_exc}"
                    )
                    tenant.jira_config = jira_config
                    tenant.updated_at = datetime.now(timezone.utc)
                    session.commit()
                    return JiraWebhookActionResult(
                        ok=False,
                        action=action_name,
                        details=jira_config["webhook_last_error"],
                        webhook_ids=_parse_managed_webhook_ids(jira_config),
                    )
        if webhook_ids is None:
            jira_config["webhook_last_error"] = f"Failed to provision Jira webhook: {exc}"
            tenant.jira_config = jira_config
            tenant.updated_at = datetime.now(timezone.utc)
            session.commit()
            return JiraWebhookActionResult(
                ok=False,
                action=action_name,
                details=jira_config["webhook_last_error"],
                webhook_ids=_parse_managed_webhook_ids(jira_config),
            )

    now_iso = datetime.now(timezone.utc).isoformat()
    jira_config["managed_webhook_ids"] = webhook_ids
    jira_config["webhook_last_provisioned_at"] = now_iso
    jira_config["webhook_last_error"] = None
    tenant.jira_config = jira_config
    tenant.updated_at = datetime.now(timezone.utc)
    session.commit()
    details = f"Provisioned {len(webhook_ids)} Jira webhook(s)."
    if cleanup_note:
        details = f"{details} {cleanup_note}"
    return JiraWebhookActionResult(
        ok=True,
        action=action_name,
        details=details,
        webhook_ids=webhook_ids,
    )


def _jira_webhook_action_status_code(result: JiraWebhookActionResult) -> int:
    return _jira_webhook_action_status_code_impl(result)


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
    )
    return RedirectResponse(url=redirect_url, status_code=status.HTTP_302_FOUND)


@router.get("/jira/connections/{connection_id}/projects", response_model=list[JiraProjectRead])
def list_jira_projects_for_connection(
    connection_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> list[JiraProjectRead]:
    connection = session.get(JiraOAuthConnection, connection_id)
    if connection is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Jira connection not found")

    settings = get_settings()
    access_token = _refresh_jira_connection_tokens(
        session,
        connection=connection,
        settings=settings,
    )
    client = _jira_oauth_client(session=session, settings=settings)
    projects = client.list_projects(access_token=access_token, cloud_id=connection.cloud_id)
    return [JiraProjectRead(key=project.key, name=project.name) for project in projects]


@router.get("/tenants/{tenant_id}/jira/webhooks/diagnostics", response_model=JiraWebhookDiagnosticsRead)
def get_jira_webhook_diagnostics(
    tenant_id: str,
    within_minutes: int = Query(default=60, ge=1, le=1440),
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> JiraWebhookDiagnosticsRead:
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    return _build_jira_webhook_diagnostics_impl(
        tenant_id=tenant_id,
        within_minutes=within_minutes,
        jira_config=dict(tenant.jira_config),
        settings=get_settings(),
        jira_webhook_callback_url_fn=_jira_webhook_callback_url,
        parse_managed_webhook_ids_fn=_parse_managed_webhook_ids,
    )


@router.post("/tenants/{tenant_id}/jira/webhooks/provision", response_model=JiraWebhookActionResult)
def provision_tenant_jira_webhook(
    tenant_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> JiraWebhookActionResult:
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    settings = get_settings()
    result = _provision_jira_webhook(
        session=session,
        tenant=tenant,
        settings=settings,
        replace_existing=False,
    )
    return JSONResponse(
        status_code=_jira_webhook_action_status_code(result),
        content=result.model_dump(),
    )


@router.post("/tenants/{tenant_id}/jira/webhooks/reset", response_model=JiraWebhookActionResult)
def reset_tenant_jira_webhook(
    tenant_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> JiraWebhookActionResult:
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    settings = get_settings()
    result = _provision_jira_webhook(
        session=session,
        tenant=tenant,
        settings=settings,
        replace_existing=True,
    )
    return JSONResponse(
        status_code=_jira_webhook_action_status_code(result),
        content=result.model_dump(),
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
    tenants = session.execute(select(Tenant).order_by(Tenant.tenant_id)).scalars().all()
    return [_tenant_to_schema(tenant) for tenant in tenants]


@router.post("/tenants", response_model=TenantRead, status_code=status.HTTP_201_CREATED)
def create_tenant(
    payload: TenantCreate,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> TenantRead:
    _validate_codex_assets_for_tenant_init()
    return _create_tenant_impl(
        session=session,
        payload=payload,
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
    return _tenant_to_schema(_get_tenant_or_404_impl(session=session, tenant_id=tenant_id))


@router.put("/tenants/{tenant_id}", response_model=TenantRead)
def update_tenant(
    tenant_id: str,
    payload: TenantUpdate,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> TenantRead:
    _validate_codex_assets_for_tenant_init()
    return _update_tenant_impl(
        session=session,
        tenant_id=tenant_id,
        payload=payload,
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
    return _delete_tenant_impl(session=session, tenant_id=tenant_id)


@router.post("/tenants/{tenant_id}/archive", response_model=TenantRead)
def archive_tenant(
    tenant_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> TenantRead:
    return _set_tenant_archive_state_impl(
        session=session,
        tenant_id=tenant_id,
        is_enabled=False,
        tenant_to_schema_fn=_tenant_to_schema,
    )


@router.post("/tenants/{tenant_id}/unarchive", response_model=TenantRead)
def unarchive_tenant(
    tenant_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> TenantRead:
    return _set_tenant_archive_state_impl(
        session=session,
        tenant_id=tenant_id,
        is_enabled=True,
        tenant_to_schema_fn=_tenant_to_schema,
    )


@router.get("/tenants/{tenant_id}/projects", response_model=list[ProjectRead])
def list_projects(
    tenant_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> list[ProjectRead]:
    service = _admin_project_service()
    return service.list_projects(session=session, tenant_id=tenant_id)  # type: ignore[return-value]


@router.post("/tenants/{tenant_id}/projects", response_model=ProjectRead, status_code=status.HTTP_201_CREATED)
def create_project(
    tenant_id: str,
    payload: ProjectCreate,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> ProjectRead:
    service = _admin_project_service()
    return service.create_project(session=session, tenant_id=tenant_id, payload=payload)  # type: ignore[return-value]


@router.get("/tenants/{tenant_id}/projects/{project_id}", response_model=ProjectRead)
def get_project(
    tenant_id: str,
    project_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> ProjectRead:
    service = _admin_project_service()
    return service.get_project(session=session, tenant_id=tenant_id, project_id=project_id)  # type: ignore[return-value]


@router.put("/tenants/{tenant_id}/projects/{project_id}", response_model=ProjectRead)
def update_project(
    tenant_id: str,
    project_id: str,
    payload: ProjectUpdate,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> ProjectRead:
    service = _admin_project_service()
    return service.update_project(session=session, tenant_id=tenant_id, project_id=project_id, payload=payload)  # type: ignore[return-value]


@router.post("/tenants/{tenant_id}/test-jira", response_model=IntegrationTestResult)
def test_jira_connection(
    tenant_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> IntegrationTestResult:
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    jira = tenant.jira_config
    required = ["project_keys"]
    missing = [field for field in required if not jira.get(field)]
    if missing:
        return IntegrationTestResult(ok=False, details=f"Missing Jira fields: {', '.join(missing)}")

    connection_id = jira.get("connection_id")
    if not isinstance(connection_id, str) or not connection_id:
        return IntegrationTestResult(ok=False, details="Jira OAuth connection is not linked for this tenant")

    connection = session.get(JiraOAuthConnection, connection_id)
    if connection is None:
        return IntegrationTestResult(ok=False, details="Configured Jira connection was not found")

    settings = get_settings()
    try:
        access_token = _refresh_jira_connection_tokens(
            session,
            connection=connection,
            settings=settings,
            tenant_id=tenant_id,
        )
        client = _jira_oauth_client(session=session, settings=settings, tenant_id=tenant_id)
        projects = client.list_projects(access_token=access_token, cloud_id=connection.cloud_id)
    except (ValueError, JiraOAuthError) as exc:
        return IntegrationTestResult(ok=False, details=f"Jira OAuth validation failed: {exc}")

    return IntegrationTestResult(
        ok=True,
        details=(
            f"Jira OAuth connection is valid for {connection.site_url}; "
            f"{len(projects)} project(s) visible"
        ),
    )


@router.post("/tenants/{tenant_id}/test-github", response_model=IntegrationTestResult)
def test_github_connection(
    tenant_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> IntegrationTestResult:
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    github = tenant.github_config
    required = ["mode"]
    missing = [field for field in required if not github.get(field)]
    if missing:
        return IntegrationTestResult(ok=False, details=f"Missing GitHub fields: {', '.join(missing)}")

    if github.get("mode") != "github_app":
        return IntegrationTestResult(ok=False, details="Only github_app mode is supported")

    if not github.get("installation_id"):
        return IntegrationTestResult(
            ok=False,
            details="GitHub App installation is not connected for this tenant",
        )

    try:
        settings = get_settings()
        github_client_from_tenant_config(
            _with_managed_github_refs(github),
            secret_lookup=lambda ref: resolve_scoped_secret_ref(
                session,
                secret_ref=ref,
                encryption_key=settings.secrets_encryption_key,
                tenant_id=tenant_id,
            ),
        )
    except ValueError as exc:
        return IntegrationTestResult(ok=False, details=str(exc))

    return IntegrationTestResult(
        ok=True,
        details="GitHub tenant configuration looks valid and secret refs resolve",
    )

@router.get("/tenants/{tenant_id}/repo-bootstrap", response_model=list[RepoBootstrapStateRead])
def list_tenant_repo_bootstrap_states(
    tenant_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> list[RepoBootstrapStateRead]:
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    states = list_repo_bootstrap_states(session=session, tenant_id=tenant_id)
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
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    return _release_bootstrap_report_from_config(tenant_id=tenant_id, jira_config=dict(tenant.jira_config or {}))


@router.post("/tenants/{tenant_id}/release/bootstrap", response_model=ReleaseBootstrapReportRead)
def run_release_bootstrap(
    tenant_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> ReleaseBootstrapReportRead:
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    settings = get_settings()
    ok, checks, details, report_payload = _compute_release_bootstrap_result_impl(
        session=session,
        tenant=tenant,
        tenant_id=tenant_id,
        settings=settings,
        required_statuses=RELEASE_BOOTSTRAP_REQUIRED_STATUSES,
        refresh_jira_connection_tokens_fn=_refresh_jira_connection_tokens,
        jira_oauth_client_fn=_jira_oauth_client,
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
        settings=get_settings(),
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
        github_client_from_tenant_config_fn=github_client_from_tenant_config,
    )


@router.get("/runs", response_model=list[RunRead])
def list_runs(
    tenant_id: str | None = Query(default=None),
    project_id: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> list[RunRead]:
    query = _build_runs_query_impl(
        tenant_id=tenant_id,
        project_id=project_id,
        status_filter=status_filter,
        from_time=from_time,
        to_time=to_time,
    )
    runs = session.execute(query).scalars().all()
    return [_run_to_schema(run) for run in runs]


@router.get("/runs/{run_id}", response_model=RunRead)
def get_run(
    run_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> RunRead:
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

    return _run_to_schema(run)
