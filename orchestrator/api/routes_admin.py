from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import delete, select
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
    ReadyIssuePreviewRead,
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
from orchestrator.core.github_install_state import create_install_state_token, parse_install_state_token
from orchestrator.core.security import require_admin
from orchestrator.storage.models import JiraOAuthConnection, Project, Run, Tenant
from orchestrator.tools.github_app import GitHubApiError, github_client_from_tenant_config
from orchestrator.tools.discord_api import DiscordApiClient, DiscordApiError
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
    if result.ok:
        return status.HTTP_200_OK
    if result.details.startswith("Jira OAuth connection is not linked"):
        return status.HTTP_400_BAD_REQUEST
    if result.details.startswith("Configured Jira connection was not found"):
        return status.HTTP_400_BAD_REQUEST
    return status.HTTP_502_BAD_GATEWAY


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

    settings = get_settings()
    jira_config = dict(tenant.jira_config)
    connection_id = jira_config.get("connection_id")
    connected = isinstance(connection_id, str) and bool(connection_id.strip())
    last_received_at_raw = jira_config.get("webhook_last_received_at")
    last_received_at = (
        last_received_at_raw.strip()
        if isinstance(last_received_at_raw, str) and last_received_at_raw.strip()
        else None
    )
    recent_delivery_ok = False
    if last_received_at:
        try:
            parsed_last_received = datetime.fromisoformat(last_received_at.replace("Z", "+00:00"))
            threshold = datetime.now(timezone.utc) - timedelta(minutes=within_minutes)
            recent_delivery_ok = parsed_last_received >= threshold
        except ValueError:
            recent_delivery_ok = False

    return JiraWebhookDiagnosticsRead(
        tenant_id=tenant_id,
        connected=connected,
        webhook_url=_jira_webhook_callback_url(settings=settings, tenant_id=tenant_id),
        managed_webhook_ids=_parse_managed_webhook_ids(jira_config),
        last_provisioned_at=jira_config.get("webhook_last_provisioned_at"),
        last_received_at=last_received_at,
        last_delivery_id=jira_config.get("webhook_last_delivery_id"),
        last_issue_key=jira_config.get("webhook_last_issue_key"),
        last_error=jira_config.get("webhook_last_error"),
        recent_delivery_window_minutes=within_minutes,
        recent_delivery_ok=recent_delivery_ok,
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
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    settings = get_settings()

    webhook_delete_ok, delete_details, _ = _delete_jira_webhooks(
        session=session,
        tenant=tenant,
        settings=settings,
    )
    session.refresh(tenant)
    jira_config = dict(tenant.jira_config)
    jira_config["connection_id"] = None
    jira_config["managed_webhook_ids"] = []
    tenant.jira_config = jira_config
    tenant.updated_at = datetime.now(timezone.utc)
    session.commit()

    details = "Jira connection disconnected and webhook metadata cleared."
    if not webhook_delete_ok:
        details = (
            "Jira connection disconnected, but webhook deletion failed. "
            f"{delete_details}"
        )
    return JiraWebhookActionResult(
        ok=True,
        action="disconnect",
        details=details,
        webhook_ids=[],
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
    project = session.get(Project, project_id)
    if project is None or project.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return _parse_discord_allowlist_requests(project.discord_config, project_id=project.project_id)


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
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    project = session.get(Project, project_id)
    if project is None or project.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    normalized_user_id = user_id.strip()
    if not normalized_user_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Discord user ID is required")

    discord_config = dict(project.discord_config or {})
    existing_requests = _parse_discord_allowlist_requests(discord_config, project_id=project.project_id)
    matching_request = next((item for item in existing_requests if item.user_id == normalized_user_id), None)
    if matching_request is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Allowlist request not found")

    allowed_user_ids_raw = discord_config.get("allowed_user_ids")
    allowed_user_ids = (
        [str(value).strip() for value in allowed_user_ids_raw if str(value).strip()]
        if isinstance(allowed_user_ids_raw, list)
        else []
    )
    if normalized_user_id not in allowed_user_ids:
        allowed_user_ids.append(normalized_user_id)
    discord_config["allowed_user_ids"] = allowed_user_ids
    discord_config["allowlist_requests"] = [
        item.model_dump()
        for item in existing_requests
        if item.user_id != normalized_user_id
    ]
    project.discord_config = discord_config
    project.updated_at = datetime.now(timezone.utc)
    tenant.updated_at = datetime.now(timezone.utc)
    session.commit()

    settings = get_settings()
    notified = False
    notify_error: str | None = None
    try:
        notified = _notify_discord_allowlist_approved(
            session=session,
            settings=settings,
            tenant_id=tenant_id,
            user_id=normalized_user_id,
        )
    except (DiscordApiError, ValueError) as exc:
        notify_error = str(exc)

    details = f"Approved allowlist request for {normalized_user_id} on project {project.name}."
    if notified:
        details = f"{details} Sent Discord DM confirmation."
    elif notify_error:
        details = f"{details} DM notification failed: {notify_error}"
    else:
        details = f"{details} DM notification skipped (bot token unavailable)."

    return DiscordAllowlistApprovalResult(
        ok=True,
        details=details,
        project_id=project.project_id,
        user_id=normalized_user_id,
        notified=notified,
    )


@router.get("/tenants/{tenant_id}/ready-preview", response_model=ReadyGatePreviewRead)
def preview_tenant_ready_gate(
    tenant_id: str,
    max_results: int = Query(default=10, ge=1, le=50),
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> ReadyGatePreviewRead:
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    jira_config = tenant.jira_config
    project_keys = jira_config.get("project_keys")
    if not isinstance(project_keys, list) or not project_keys:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing Jira project_keys")

    raw_ready_statuses = jira_config.get("ready_statuses")
    if isinstance(raw_ready_statuses, list):
        ready_statuses = [str(value).strip() for value in raw_ready_statuses if str(value).strip()]
    else:
        ready_statuses = []
    if not ready_statuses:
        ready_statuses = ["Ready for Agent"]

    raw_ready_jql = jira_config.get("ready_jql")
    ready_jql = raw_ready_jql.strip() if isinstance(raw_ready_jql, str) else ""
    if not ready_jql:
        ready_jql = _default_ready_jql(project_keys=project_keys, ready_statuses=ready_statuses)

    connection_id = jira_config.get("connection_id")
    if not isinstance(connection_id, str) or not connection_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Jira OAuth connection is not linked")
    connection = session.get(JiraOAuthConnection, connection_id)
    if connection is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Configured Jira connection was not found")

    settings = get_settings()
    try:
        access_token = _refresh_jira_connection_tokens(
            session,
            connection=connection,
            settings=settings,
            tenant_id=tenant_id,
        )
        client = _jira_oauth_client(session=session, settings=settings, tenant_id=tenant_id)
        issues = client.search_issues_by_jql(
            access_token=access_token,
            cloud_id=connection.cloud_id,
            jql=ready_jql,
            max_results=max_results,
        )
    except (ValueError, JiraOAuthError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Ready preview failed: {exc}") from exc

    guidance = (
        "Issues are executable only when they are in a configured ready status. "
        "If an issue is missing here, move it to a ready status and retry."
    )
    return ReadyGatePreviewRead(
        ready_statuses=ready_statuses,
        ready_jql=ready_jql,
        eligible_issues=[
            ReadyIssuePreviewRead(key=issue.key, summary=issue.summary, status=issue.status)
            for issue in issues
        ],
        guidance=guidance,
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
    tenant_id = _allocate_tenant_id(session, name=payload.name)
    now = datetime.now(timezone.utc)
    tenant = Tenant(
        tenant_id=tenant_id,
        name=payload.name,
        is_enabled=payload.is_enabled,
        jira_config=_with_preserved_jira_system_fields(
            existing={},
            proposed=payload.jira.model_dump(),
        ),
        github_config=_with_managed_github_refs(payload.github.model_dump()),
        repos_config=payload.repos.model_dump(),
        policy_config=payload.policy.model_dump(),
        discord_config=_with_preserved_discord_system_fields(
            existing={},
            proposed=payload.discord.model_dump() if payload.discord else None,
        ),
        created_at=now,
        updated_at=now,
    )
    session.add(tenant)
    _ensure_default_project_for_tenant(session, tenant=tenant)
    _sync_tenant_jira_project_keys(session, tenant=tenant)
    session.commit()
    session.refresh(tenant)
    return _tenant_to_schema(tenant)


@router.get("/tenants/{tenant_id}", response_model=TenantRead)
def get_tenant(
    tenant_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> TenantRead:
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    return _tenant_to_schema(tenant)


@router.put("/tenants/{tenant_id}", response_model=TenantRead)
def update_tenant(
    tenant_id: str,
    payload: TenantUpdate,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> TenantRead:
    _validate_codex_assets_for_tenant_init()
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    tenant.name = payload.name
    tenant.is_enabled = payload.is_enabled
    tenant.jira_config = _with_preserved_jira_system_fields(
        existing=dict(tenant.jira_config),
        proposed=payload.jira.model_dump(),
    )
    tenant.github_config = _with_managed_github_refs(payload.github.model_dump())
    tenant.repos_config = payload.repos.model_dump()
    tenant.policy_config = payload.policy.model_dump()
    tenant.discord_config = _with_preserved_discord_system_fields(
        existing=dict(tenant.discord_config or {}),
        proposed=payload.discord.model_dump() if payload.discord else None,
    )
    tenant.updated_at = datetime.now(timezone.utc)
    _ensure_default_project_for_tenant(session, tenant=tenant)
    _sync_tenant_jira_project_keys(session, tenant=tenant)

    session.commit()
    session.refresh(tenant)
    return _tenant_to_schema(tenant)


@router.delete("/tenants/{tenant_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_tenant(
    tenant_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> Response:
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    session.execute(delete(Run).where(Run.tenant_id == tenant_id))
    session.delete(tenant)
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/tenants/{tenant_id}/archive", response_model=TenantRead)
def archive_tenant(
    tenant_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> TenantRead:
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    tenant.is_enabled = False
    tenant.updated_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(tenant)
    return _tenant_to_schema(tenant)


@router.post("/tenants/{tenant_id}/unarchive", response_model=TenantRead)
def unarchive_tenant(
    tenant_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> TenantRead:
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    tenant.is_enabled = True
    tenant.updated_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(tenant)
    return _tenant_to_schema(tenant)


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
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    github = tenant.github_config
    if github.get("mode") != "github_app":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only github_app mode is supported",
        )

    settings = get_settings()
    app_slug = settings.github_app_slug.strip()
    if not app_slug:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="GitHub app slug is not configured",
        )

    expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)
    state_token = create_install_state_token(
        tenant_id=tenant_id,
        exp=expires_at,
        secret=settings.github_install_state_secret,
        return_to=return_to,
    )
    install_url = (
        f"https://github.com/apps/{quote(app_slug, safe='')}/installations/new?"
        f"state={quote(state_token, safe='')}"
    )
    return GitHubInstallStart(install_url=install_url, expires_at=expires_at)


@router.get("/github/install/callback", include_in_schema=False)
def github_install_callback(
    state_token: str = Query(..., alias="state"),
    installation_id: str = Query(..., min_length=1),
    setup_action: str | None = Query(default=None),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    settings = get_settings()
    try:
        state = parse_install_state_token(
            token=state_token,
            secret=settings.github_install_state_secret,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    tenant = session.get(Tenant, state.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    github_config = dict(tenant.github_config)
    github_config["installation_id"] = str(installation_id)
    if setup_action:
        github_config["installation_setup_action"] = setup_action
    github_config["installation_updated_at"] = datetime.now(timezone.utc).isoformat()
    tenant.github_config = github_config
    tenant.updated_at = datetime.now(timezone.utc)
    session.commit()

    if state.return_to == "wizard":
        redirect_url = (
            f"{settings.admin_ui_base_url.rstrip('/')}/tenants/new"
            f"?tenant_id={quote(tenant.tenant_id, safe='')}&github_install=success"
        )
    else:
        redirect_url = (
            f"{settings.admin_ui_base_url.rstrip('/')}/tenants/{quote(tenant.tenant_id, safe='')}/edit"
            "?github_install=success"
        )
    return RedirectResponse(url=redirect_url, status_code=status.HTTP_302_FOUND)


@router.get("/tenants/{tenant_id}/github/repositories", response_model=list[GitHubRepositoryRead])
def list_tenant_github_repositories(
    tenant_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> list[GitHubRepositoryRead]:
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    github = tenant.github_config
    if github.get("mode") != "github_app":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only github_app mode is supported")
    if not github.get("installation_id"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="GitHub App installation is not connected for this tenant",
        )

    try:
        settings = get_settings()
        client = github_client_from_tenant_config(
            _with_managed_github_refs(github),
            secret_lookup=lambda ref: resolve_scoped_secret_ref(
                session,
                secret_ref=ref,
                encryption_key=settings.secrets_encryption_key,
                tenant_id=tenant_id,
            ),
        )
        repositories = client.list_installation_repositories()
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except GitHubApiError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    return [
        GitHubRepositoryRead(
            full_name=repo.full_name,
            html_url=repo.html_url,
            default_branch=repo.default_branch,
            private=repo.private,
        )
        for repo in repositories
    ]


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
    query = select(Run).order_by(Run.created_at.desc())

    if tenant_id:
        query = query.where(Run.tenant_id == tenant_id)
    if project_id:
        query = query.where(Run.project_id == project_id)
    if status_filter:
        query = query.where(Run.status == status_filter)
    if from_time:
        query = query.where(Run.created_at >= from_time)
    if to_time:
        query = query.where(Run.created_at <= to_time)

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
