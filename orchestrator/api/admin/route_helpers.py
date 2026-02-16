from __future__ import annotations

import logging
from urllib.error import URLError
from urllib.parse import quote_plus

from sqlalchemy.orm import Session

from orchestrator.api.admin.config_helpers import (
    validate_codex_assets_for_tenant_init as _validate_codex_assets_for_tenant_init_impl,
    with_managed_github_refs as _with_managed_github_refs_impl,
    with_preserved_jira_system_fields as _with_preserved_jira_system_fields_impl,
)
from orchestrator.api.admin.discord_allowlist_helpers import (
    notify_discord_allowlist_approved as _notify_discord_allowlist_approved_impl,
    parse_discord_allowlist_requests as _parse_discord_allowlist_requests_impl,
)
from orchestrator.api.admin.jira_oauth_helpers import (
    jira_oauth_client as _jira_oauth_client_impl,
    refresh_jira_connection_tokens as _refresh_jira_connection_tokens_impl,
    resolve_secret_ref as _resolve_secret_ref_impl,
)
from orchestrator.api.admin.jira_webhook_cleanup import (
    all_managed_webhook_ids as _all_managed_webhook_ids_impl,
    cleanup_conflicting_jira_webhook_url as _cleanup_conflicting_jira_webhook_url_impl,
    cleanup_unmanaged_jira_webhooks_for_connection as _cleanup_unmanaged_jira_webhooks_for_connection_impl,
    remove_managed_webhook_id_from_tenants as _remove_managed_webhook_id_from_tenants_impl,
)
from orchestrator.api.admin.jira_webhook_delete import (
    delete_jira_webhooks as _delete_jira_webhooks_impl,
)
from orchestrator.api.admin.jira_webhook_helpers import (
    extract_jira_webhook_conflict_url as _extract_jira_webhook_conflict_url,
    is_jira_webhook_limit_error as _is_jira_webhook_limit_error,
    is_jira_webhook_single_url_error as _is_jira_webhook_single_url_error,
    jira_webhook_callback_url as _jira_webhook_callback_url,
    jira_webhook_filter_jql as _jira_webhook_filter_jql,
    parse_jira_webhook_id as _parse_jira_webhook_id,
    parse_managed_webhook_ids as _parse_managed_webhook_ids,
)
from orchestrator.api.admin.jira_webhook_provision import (
    provision_jira_webhook as _provision_jira_webhook_impl,
)
from orchestrator.api.admin.jira_webhook_response_helpers import (
    jira_webhook_action_status_code as _jira_webhook_action_status_code_impl,
)
from orchestrator.api.admin.project_normalization import (
    default_project_name_from_repo as _default_project_name_from_repo,
    normalize_project_discord_config as _normalize_project_discord_config,
    normalize_project_key as _normalize_project_key,
    normalize_project_repo as _normalize_project_repo,
    normalize_string_map as _normalize_string_map,
    resolve_project_discord_channel_name as _resolve_project_discord_channel_name,
    with_preserved_discord_system_fields as _with_preserved_discord_system_fields,
)
from orchestrator.api.admin.project_service import AdminProjectService
from orchestrator.api.admin.schema_mappers import project_to_schema as _project_to_schema
from orchestrator.api.admin.tenant_project_helpers import (
    allocate_tenant_id as _allocate_tenant_id_impl,
    ensure_default_project_for_tenant as _ensure_default_project_for_tenant_impl,
    primary_jira_project_key as _primary_jira_project_key_impl,
    primary_repo_url as _primary_repo_url_impl,
    resolve_project_discord_channel_binding as _resolve_project_discord_channel_binding_impl,
    slugify_tenant_name as _slugify_tenant_name_impl,
    sync_tenant_jira_project_keys as _sync_tenant_jira_project_keys_impl,
)
from orchestrator.api.schemas import (
    DiscordAllowlistRequestRead,
    JiraWebhookActionResult,
)
from orchestrator.core.config import get_settings
from orchestrator.core.project_policy import normalize_project_policy_overrides
from orchestrator.core.platform_secret_service import resolve_platform_secret_ref
from orchestrator.core.platform_secret_service import (
    PLATFORM_SECRET_GITHUB_APP_ID_REF,
    PLATFORM_SECRET_GITHUB_PRIVATE_KEY_REF,
)
from orchestrator.core.tenant_secret_service import resolve_scoped_secret_ref
from orchestrator.storage.models import JiraOAuthConnection, Project, Tenant
from orchestrator.tools.discord_api import DiscordApiClient
from orchestrator.tools.github_app import GitHubApiError, github_client_from_tenant_config
from orchestrator.tools.jira_oauth import JiraOAuthClient
from orchestrator.tools.jira_oauth import JiraOAuthError
from orchestrator.tools.jira_oauth_http import JiraOAuthHttpClient
from orchestrator.tools.project_repo_checkout import ensure_project_checkout
from orchestrator.tools.project_repo_checkout import ProjectRepoCheckoutError

logger = logging.getLogger(__name__)
_MAX_BOARD_DISCOVERY_SCAN = 200


def _parse_board_id(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _is_kanban_board_type(value: object) -> bool:
    normalized = str(value or "").strip().lower()
    return normalized in {"kanban", "simple"}


def slugify_tenant_name(name: str) -> str:
    return _slugify_tenant_name_impl(name)


def allocate_tenant_id(session: Session, *, name: str) -> str:
    return _allocate_tenant_id_impl(session, name=name)


def primary_jira_project_key(jira_config: dict) -> str | None:
    return _primary_jira_project_key_impl(jira_config)


def primary_repo_url(repos_config: dict) -> str | None:
    return _primary_repo_url_impl(repos_config)


def resolve_project_discord_channel_binding(
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


def ensure_default_project_for_tenant(session: Session, *, tenant: Tenant) -> None:
    _ensure_default_project_for_tenant_impl(
        session,
        tenant=tenant,
        default_project_name_from_repo_fn=_default_project_name_from_repo,
    )


def sync_tenant_jira_project_keys(session: Session, *, tenant: Tenant) -> None:
    _sync_tenant_jira_project_keys_impl(
        session,
        tenant=tenant,
        normalize_project_key_fn=_normalize_project_key,
    )


def with_managed_github_refs(raw_github_config: dict) -> dict:
    return _with_managed_github_refs_impl(
        raw_github_config=raw_github_config,
        settings=get_settings(),
    )


def validate_codex_assets_for_tenant_init() -> None:
    _validate_codex_assets_for_tenant_init_impl(
        settings=get_settings(),
        module_file=__file__,
    )


def resolve_secret_ref(
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


def jira_oauth_client(
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


def refresh_jira_connection_tokens(
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
        jira_oauth_client_fn=jira_oauth_client,
    )


def all_managed_webhook_ids(session: Session) -> set[int]:
    return _all_managed_webhook_ids_impl(
        session=session,
        parse_managed_webhook_ids_fn=_parse_managed_webhook_ids,
    )


def cleanup_unmanaged_jira_webhooks_for_connection(
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


def cleanup_conflicting_jira_webhook_url(
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
        remove_managed_webhook_id_from_tenants_fn=remove_managed_webhook_id_from_tenants,
    )


def remove_managed_webhook_id_from_tenants(*, session: Session, webhook_id: int) -> int:
    return _remove_managed_webhook_id_from_tenants_impl(
        session=session,
        webhook_id=webhook_id,
        parse_managed_webhook_ids_fn=_parse_managed_webhook_ids,
    )


def with_preserved_jira_system_fields(*, existing: dict, proposed: dict) -> dict:
    return _with_preserved_jira_system_fields_impl(existing=existing, proposed=proposed)


def parse_discord_allowlist_requests(
    discord_config: dict | None,
    *,
    project_id: str | None = None,
) -> list[DiscordAllowlistRequestRead]:
    return _parse_discord_allowlist_requests_impl(
        discord_config,
        project_id=project_id,
    )


def notify_discord_allowlist_approved(
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
        resolve_secret_ref_fn=resolve_platform_secret_ref,
        discord_client_factory=DiscordApiClient,
    )


def delete_jira_webhooks(
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
        refresh_jira_connection_tokens_fn=refresh_jira_connection_tokens,
        jira_oauth_client_fn=jira_oauth_client,
    )


def admin_project_service() -> AdminProjectService:
    return AdminProjectService(
        normalize_project_repo=_normalize_project_repo,
        normalize_project_key=_normalize_project_key,
        normalize_project_policy_overrides=normalize_project_policy_overrides,
        normalize_string_map=_normalize_string_map,
        normalize_project_discord_config=_normalize_project_discord_config,
        with_preserved_discord_system_fields=_with_preserved_discord_system_fields,
        resolve_project_discord_channel_binding=resolve_project_discord_channel_binding,
        sync_tenant_jira_project_keys=sync_tenant_jira_project_keys,
        ensure_project_repository_checkout=ensure_project_repository_checkout,
        resolve_project_run_board_id=discover_project_run_board_id,
        project_to_schema=_project_to_schema,
        settings_factory=get_settings,
    )


def discover_project_run_board_id(
    *,
    session: Session,
    tenant: Tenant,
    jira_project_key: str,
    settings,  # noqa: ANN001
) -> int | None:
    connection_id = str((tenant.jira_config or {}).get("connection_id") or "").strip()
    if not connection_id:
        return None
    connection = session.get(JiraOAuthConnection, connection_id)
    if connection is None:
        logger.warning(
            "project_board_discovery_skipped tenant_id=%s jira_project_key=%s reason=connection_missing connection_id=%s",
            tenant.tenant_id,
            jira_project_key,
            connection_id,
        )
        return None

    access_token = refresh_jira_connection_tokens(
        session,
        connection=connection,
        settings=settings,
        tenant_id=tenant.tenant_id,
    )
    http_client = JiraOAuthHttpClient()
    cloud_id = connection.cloud_id
    project_key = str(jira_project_key or "").strip().upper()
    if not project_key:
        raise ValueError("Missing Jira project key")

    project_url = f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/project/{quote_plus(project_key)}"
    try:
        project_payload = http_client.get_json(url=project_url, access_token=access_token)
    except (JiraOAuthError, URLError, ValueError) as exc:
        logger.warning(
            "project_board_discovery_skipped tenant_id=%s jira_project_key=%s reason=project_metadata_error error=%s",
            tenant.tenant_id,
            jira_project_key,
            exc,
        )
        return None
    if not isinstance(project_payload, dict):
        return None

    target_project_id = str(project_payload.get("id") or "").strip()
    if not target_project_id:
        return None

    list_url = (
        f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/agile/1.0/board"
        f"?projectKeyOrId={quote_plus(project_key)}&maxResults=1"
    )
    try:
        list_payload = http_client.get_json(url=list_url, access_token=access_token)
        if isinstance(list_payload, dict):
            values = list_payload.get("values")
            if isinstance(values, list):
                fallback_board_id: int | None = None
                for item in values:
                    if not isinstance(item, dict):
                        continue
                    board_id = _parse_board_id(item.get("id"))
                    if board_id is None:
                        continue
                    location = item.get("location")
                    location_project_id = (
                        str(location.get("projectId") or "").strip()
                        if isinstance(location, dict)
                        else ""
                    )
                    if location_project_id and location_project_id != target_project_id:
                        continue
                    if _is_kanban_board_type(item.get("type")):
                        return board_id
                    if fallback_board_id is None:
                        fallback_board_id = board_id
                if fallback_board_id is not None:
                    return fallback_board_id
    except (JiraOAuthError, URLError, ValueError) as exc:
        logger.warning(
            "project_board_discovery_skipped tenant_id=%s jira_project_key=%s reason=board_list_lookup_failed error=%s",
            tenant.tenant_id,
            jira_project_key,
            exc,
        )

    for board_id in range(1, _MAX_BOARD_DISCOVERY_SCAN + 1):
        board_url = f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/agile/1.0/board/{board_id}"
        try:
            board_payload = http_client.get_json(url=board_url, access_token=access_token)
        except (JiraOAuthError, URLError, ValueError):
            continue
        if not isinstance(board_payload, dict):
            continue
        location = board_payload.get("location")
        if not isinstance(location, dict):
            continue
        location_project_id = str(location.get("projectId") or "").strip()
        if location_project_id == target_project_id:
            return board_id

    logger.warning(
        "project_board_discovery_skipped tenant_id=%s jira_project_key=%s reason=not_found scan_limit=%s",
        tenant.tenant_id,
        jira_project_key,
        _MAX_BOARD_DISCOVERY_SCAN,
    )
    return None


def ensure_project_repository_checkout(*, session: Session, tenant: Tenant, project: Project) -> None:
    settings = get_settings()
    github_config = tenant.github_config or {}
    app_id_ref = str(github_config.get("app_id_ref") or PLATFORM_SECRET_GITHUB_APP_ID_REF).strip()
    private_key_ref = str(github_config.get("private_key_ref") or PLATFORM_SECRET_GITHUB_PRIVATE_KEY_REF).strip()

    tenant_id = tenant.tenant_id
    app_id = (
        resolve_scoped_secret_ref(
            session,
            secret_ref=app_id_ref,
            encryption_key=settings.secrets_encryption_key,
            tenant_id=tenant_id,
        )
        if app_id_ref.startswith(("tenant/", "project/"))
        else resolve_platform_secret_ref(
            session,
            secret_ref=app_id_ref,
            encryption_key=settings.secrets_encryption_key,
        )
    )
    private_key = (
        resolve_scoped_secret_ref(
            session,
            secret_ref=private_key_ref,
            encryption_key=settings.secrets_encryption_key,
            tenant_id=tenant_id,
        )
        if private_key_ref.startswith(("tenant/", "project/"))
        else resolve_platform_secret_ref(
            session,
            secret_ref=private_key_ref,
            encryption_key=settings.secrets_encryption_key,
        )
    )
    if not app_id or not private_key:
        raise ProjectRepoCheckoutError(
            "GitHub App secrets are unavailable for repository checkout"
        )
    try:
        github_client = github_client_from_tenant_config(
            github_config,
            tenant_secret_lookup=lambda secret_ref: resolve_scoped_secret_ref(
                session,
                secret_ref=secret_ref,
                encryption_key=settings.secrets_encryption_key,
                tenant_id=tenant_id,
            ),
            platform_secret_lookup=lambda secret_ref: resolve_platform_secret_ref(
                session,
                secret_ref=secret_ref,
                encryption_key=settings.secrets_encryption_key,
            ),
        )
    except ValueError as exc:
        raise ProjectRepoCheckoutError(str(exc)) from exc

    try:
        installation_token = github_client.get_installation_token()
        ensure_project_checkout(
            base_dir=settings.project_repo_checkout_base_dir,
            tenant_id=tenant.tenant_id,
            project=project,
            github_installation_token=installation_token,
        )
    except (GitHubApiError, ProjectRepoCheckoutError) as exc:
        logger.exception(
            "project_repository_checkout_failed tenant_id=%s project_id=%s",
            tenant.tenant_id,
            project.project_id,
        )
        raise ProjectRepoCheckoutError(str(exc)) from exc


def provision_jira_webhook(
    *,
    session: Session,
    tenant: Tenant,
    settings,  # noqa: ANN001
    replace_existing: bool,
    refresh_jira_connection_tokens_fn=refresh_jira_connection_tokens,
    jira_oauth_client_fn=jira_oauth_client,
    cleanup_unmanaged_jira_webhooks_for_connection_fn=cleanup_unmanaged_jira_webhooks_for_connection,
    remove_managed_webhook_id_from_tenants_fn=remove_managed_webhook_id_from_tenants,
    cleanup_conflicting_jira_webhook_url_fn=cleanup_conflicting_jira_webhook_url,
) -> JiraWebhookActionResult:
    return _provision_jira_webhook_impl(
        session=session,
        tenant=tenant,
        settings=settings,
        replace_existing=replace_existing,
        jira_webhook_events=JIRA_WEBHOOK_EVENTS,
        delete_jira_webhooks_fn=delete_jira_webhooks,
        parse_managed_webhook_ids_fn=_parse_managed_webhook_ids,
        refresh_jira_connection_tokens_fn=refresh_jira_connection_tokens_fn,
        jira_oauth_client_fn=jira_oauth_client_fn,
        jira_webhook_callback_url_fn=_jira_webhook_callback_url,
        jira_webhook_filter_jql_fn=_jira_webhook_filter_jql,
        is_jira_webhook_limit_error_fn=_is_jira_webhook_limit_error,
        cleanup_unmanaged_jira_webhooks_for_connection_fn=cleanup_unmanaged_jira_webhooks_for_connection_fn,
        parse_jira_webhook_id_fn=_parse_jira_webhook_id,
        remove_managed_webhook_id_from_tenants_fn=remove_managed_webhook_id_from_tenants_fn,
        is_jira_webhook_single_url_error_fn=_is_jira_webhook_single_url_error,
        extract_jira_webhook_conflict_url_fn=_extract_jira_webhook_conflict_url,
        cleanup_conflicting_jira_webhook_url_fn=cleanup_conflicting_jira_webhook_url_fn,
    )


def jira_webhook_action_status_code(result: JiraWebhookActionResult) -> int:
    return _jira_webhook_action_status_code_impl(result)

JIRA_WEBHOOK_EVENTS = [
    "jira:issue_created",
    "jira:issue_updated",
    "jira:issue_deleted",
    "comment_created",
    "comment_updated",
]
