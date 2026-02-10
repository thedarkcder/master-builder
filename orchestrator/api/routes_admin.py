from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from orchestrator.api.dependencies import get_session
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
from orchestrator.core.project_policy import normalize_project_policy_overrides, resolve_effective_policy
from orchestrator.core.secret_manager import (
    resolve_scoped_secret_ref,
)
from orchestrator.core.jira_oauth_state import (
    create_jira_oauth_state_token,
    parse_jira_oauth_state_token,
)
from orchestrator.core.secrets import decrypt_value, encrypt_value
from orchestrator.core.github_install_state import create_install_state_token, parse_install_state_token
from orchestrator.core.security import require_admin
from orchestrator.storage.models import JiraOAuthConnection, Project, Run, Tenant
from orchestrator.tools.github_app import GitHubApiError, github_client_from_tenant_config
from orchestrator.tools.discord_api import DiscordApiClient, DiscordApiError, DiscordTextChannel
from orchestrator.tools.jira_oauth import JiraOAuthClient, JiraOAuthClientConfig, JiraOAuthError
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


def _tenant_to_schema(tenant: Tenant) -> TenantRead:
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


def _run_to_schema(run: Run) -> RunRead:
    return RunRead(
        run_id=run.run_id,
        tenant_id=run.tenant_id,
        project_id=run.project_id,
        issue_key=run.issue_key,
        repo_url=run.repo_url,
        branch=run.branch,
        pr_url=run.pr_url,
        status=run.status,
        last_error=run.last_error,
        plan=run.plan,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


def _project_to_schema(project: Project, *, tenant_policy: dict) -> ProjectRead:
    normalized_project_discord = _normalize_project_discord_config(project.discord_config)
    return ProjectRead(
        project_id=project.project_id,
        tenant_id=project.tenant_id,
        name=project.name,
        github_repository=project.github_repository,
        jira_project_key=project.jira_project_key,
        policy_overrides=normalize_project_policy_overrides(project.policy_overrides),
        environment=dict(project.environment or {}),
        secret_refs=dict(project.secret_refs or {}),
        discord=normalized_project_discord if normalized_project_discord else None,
        effective_policy=resolve_effective_policy(
            tenant_policy=tenant_policy,
            project_overrides=project.policy_overrides,
        ),
        is_archived=project.is_archived,
        created_at=project.created_at,
        updated_at=project.updated_at,
    )


def _slugify_tenant_name(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "tenant"


def _allocate_tenant_id(session: Session, *, name: str) -> str:
    base = _slugify_tenant_name(name)
    candidate = base
    suffix = 2
    while session.get(Tenant, candidate) is not None:
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def _primary_jira_project_key(jira_config: dict) -> str | None:
    project_keys = jira_config.get("project_keys")
    if isinstance(project_keys, list):
        for key in project_keys:
            key_normalized = str(key).strip().upper()
            if key_normalized:
                return key_normalized
    return None


def _primary_repo_url(repos_config: dict) -> str | None:
    candidates: list[object] = [
        repos_config.get("github_repository"),
        repos_config.get("fallback_repo"),
    ]

    allowlist = repos_config.get("allowlist")
    if isinstance(allowlist, list):
        candidates.extend(allowlist)

    by_project = repos_config.get("mapping_rules_by_project_key")
    if isinstance(by_project, dict):
        candidates.extend(by_project.values())

    by_component = repos_config.get("mapping_rules_by_component")
    if isinstance(by_component, dict):
        candidates.extend(by_component.values())

    for candidate in candidates:
        if candidate is None:
            continue
        normalized = str(candidate).strip()
        if normalized and normalized.lower() != "none":
            return normalized
    return None


def _resolve_project_discord_channel_binding(
    *,
    session: Session,
    settings,  # noqa: ANN001
    tenant: Tenant,
    project: Project,
    discord_config: dict,
) -> dict:
    normalized = dict(discord_config or {})
    existing_channel_id = str(normalized.get("channel_id") or "").strip()
    if existing_channel_id:
        normalized["channel_id"] = existing_channel_id
        return normalized

    token_ref = settings.discord_bot_token_secret_ref.strip()
    if not token_ref:
        raise ValueError("Discord bot token reference is not configured")
    bot_token = resolve_scoped_secret_ref(
        session,
        secret_ref=token_ref,
        encryption_key=settings.secrets_encryption_key,
        tenant_id=tenant.tenant_id,
        project_id=project.project_id,
    )
    if not bot_token:
        raise ValueError(f"Discord bot token secret is missing: {token_ref}")

    guild_id = settings.discord_guild_id.strip()
    if not guild_id:
        guild_ref = settings.discord_guild_id_secret_ref.strip()
        if guild_ref:
            guild_id = (
                resolve_scoped_secret_ref(
                    session,
                    secret_ref=guild_ref,
                    encryption_key=settings.secrets_encryption_key,
                    tenant_id=tenant.tenant_id,
                    project_id=project.project_id,
                )
                or ""
            ).strip()
    if not guild_id:
        raise ValueError("Discord guild ID is not configured")

    parent_id = settings.discord_channel_category_id.strip() or None
    channel_name = _resolve_project_discord_channel_name(settings=settings, tenant=tenant, project=project)
    client = DiscordApiClient(bot_token=bot_token)
    channel: DiscordTextChannel = client.ensure_text_channel(
        guild_id=guild_id,
        name=channel_name,
        parent_id=parent_id,
    )
    normalized["channel_id"] = channel.channel_id
    return normalized


def _ensure_default_project_for_tenant(session: Session, *, tenant: Tenant) -> None:
    existing = session.execute(
        select(Project).where(Project.tenant_id == tenant.tenant_id).limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        return

    repo_url = _primary_repo_url(dict(tenant.repos_config))
    jira_project_key = _primary_jira_project_key(dict(tenant.jira_config))
    if not repo_url or not jira_project_key:
        return

    now = datetime.now(timezone.utc)
    session.add(
        Project(
            project_id=f"{tenant.tenant_id}-default",
            tenant_id=tenant.tenant_id,
            name=_default_project_name_from_repo(repo_url=repo_url, tenant_id=tenant.tenant_id),
            github_repository=repo_url,
            jira_project_key=jira_project_key,
            policy_overrides={},
            environment={},
            secret_refs={},
            is_archived=False,
            created_at=now,
            updated_at=now,
        )
    )


def _sync_tenant_jira_project_keys(session: Session, *, tenant: Tenant) -> None:
    persisted_projects = session.execute(
        select(Project)
        .where(Project.tenant_id == tenant.tenant_id, Project.is_archived.is_(False))
        .order_by(Project.created_at.asc())
    ).scalars().all()
    pending_projects = [
        project
        for project in session.new
        if isinstance(project, Project)
        and project.tenant_id == tenant.tenant_id
        and not project.is_archived
    ]
    persisted_ids = {item.project_id for item in persisted_projects}
    projects = persisted_projects + [project for project in pending_projects if project.project_id not in persisted_ids]
    keys: list[str] = []
    for project in projects:
        if project.is_archived:
            continue
        normalized = _normalize_project_key(project.jira_project_key)
        if normalized and normalized not in keys:
            keys.append(normalized)
    jira_config = dict(tenant.jira_config)
    jira_config["project_keys"] = keys
    tenant.jira_config = jira_config


def _with_managed_github_refs(raw_github_config: dict) -> dict:
    settings = get_settings()
    github_config = dict(raw_github_config)
    github_config["app_id_ref"] = settings.github_app_id_ref
    github_config["private_key_ref"] = settings.github_private_key_ref
    return github_config


def _validate_codex_assets_for_tenant_init() -> None:
    settings = get_settings()
    repo_root = Path(__file__).resolve().parents[2]
    try:
        validate_enforcement_assets(
            repo_root=repo_root,
            required_assets_version=settings.required_codex_assets_version,
        )
    except EnforcementAssetsError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Codex assets validation failed: {exc}",
        ) from exc


def _resolve_secret_ref(
    session: Session,
    *,
    ref_name: str,
    settings,
    tenant_id: str | None = None,
    project_id: str | None = None,
) -> str:  # noqa: ANN001
    value = resolve_scoped_secret_ref(
        session,
        secret_ref=str(ref_name),
        encryption_key=settings.secrets_encryption_key,
        tenant_id=tenant_id,
        project_id=project_id,
    )
    if not value:
        raise ValueError(f"Missing secret value for ref '{ref_name}'")
    return value


def _jira_oauth_client(
    *,
    session: Session,
    settings,
    tenant_id: str | None = None,
    project_id: str | None = None,
) -> JiraOAuthClient:  # noqa: ANN001
    client_id = _resolve_secret_ref(
        session,
        ref_name=settings.jira_oauth_client_id_ref,
        settings=settings,
        tenant_id=tenant_id,
        project_id=project_id,
    )
    client_secret = _resolve_secret_ref(
        session,
        ref_name=settings.jira_oauth_client_secret_ref,
        settings=settings,
        tenant_id=tenant_id,
        project_id=project_id,
    )
    redirect_uri = f"{settings.public_api_base_url.rstrip('/')}/api/admin/jira/connect/callback"
    return JiraOAuthClient(
        JiraOAuthClientConfig(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
        )
    )


def _refresh_jira_connection_tokens(
    session: Session,
    *,
    connection: JiraOAuthConnection,
    settings,
    tenant_id: str | None = None,
) -> str:  # noqa: ANN001
    now = datetime.now(timezone.utc)
    if connection.access_token_expires_at - now > timedelta(seconds=60):
        return decrypt_value(
            ciphertext=connection.access_token_encrypted,
            encryption_key=settings.secrets_encryption_key,
        )

    client = _jira_oauth_client(session=session, settings=settings, tenant_id=tenant_id)
    refresh_token = decrypt_value(
        ciphertext=connection.refresh_token_encrypted,
        encryption_key=settings.secrets_encryption_key,
    )
    token_set = client.refresh_tokens(refresh_token=refresh_token)
    connection.access_token_encrypted = encrypt_value(
        plaintext=token_set.access_token,
        encryption_key=settings.secrets_encryption_key,
    )
    connection.refresh_token_encrypted = encrypt_value(
        plaintext=token_set.refresh_token,
        encryption_key=settings.secrets_encryption_key,
    )
    connection.access_token_expires_at = token_set.expires_at
    connection.scopes = token_set.scopes
    connection.updated_at = now
    session.commit()
    return token_set.access_token


def _all_managed_webhook_ids(session: Session) -> set[int]:
    managed: set[int] = set()
    tenants = session.execute(select(Tenant.jira_config)).all()
    for (jira_config_raw,) in tenants:
        if not isinstance(jira_config_raw, dict):
            continue
        managed.update(_parse_managed_webhook_ids(jira_config_raw))
    return managed


def _cleanup_unmanaged_jira_webhooks_for_connection(
    *,
    session: Session,
    client: JiraOAuthClient,
    access_token: str,
    cloud_id: str,
) -> tuple[int, str]:
    webhooks = client.list_webhooks(access_token=access_token, cloud_id=cloud_id)
    if not webhooks:
        return 0, "No existing Jira webhooks were listed for this app/user."

    managed_ids = _all_managed_webhook_ids(session)
    stale_ids: list[int] = []
    for item in webhooks:
        webhook_id = _parse_jira_webhook_id(item.get("id"))
        if webhook_id is None:
            continue
        if webhook_id not in managed_ids:
            stale_ids.append(webhook_id)

    if not stale_ids:
        return 0, "No unmanaged Jira webhooks were found to clean up."

    client.delete_webhooks(
        access_token=access_token,
        cloud_id=cloud_id,
        webhook_ids=stale_ids,
    )
    return len(stale_ids), f"Deleted {len(stale_ids)} unmanaged Jira webhook(s)."


def _cleanup_conflicting_jira_webhook_url(
    *,
    session: Session,
    client: JiraOAuthClient,
    access_token: str,
    cloud_id: str,
    callback_url: str,
    conflicting_url: str | None,
) -> tuple[int, str]:
    target_urls = {
        callback_url.strip().lower().rstrip("/"),
    }
    if conflicting_url:
        target_urls.add(conflicting_url.strip().lower().rstrip("/"))

    webhooks = client.list_webhooks(access_token=access_token, cloud_id=cloud_id)
    delete_ids: list[int] = []
    for item in webhooks:
        webhook_id = _parse_jira_webhook_id(item.get("id"))
        webhook_url = str(item.get("url") or "").strip().lower().rstrip("/")
        if webhook_id is None or not webhook_url:
            continue
        if webhook_url in target_urls:
            delete_ids.append(webhook_id)

    if not delete_ids:
        return 0, "No conflicting Jira webhook URL was found to delete."

    client.delete_webhooks(
        access_token=access_token,
        cloud_id=cloud_id,
        webhook_ids=delete_ids,
    )
    touched_tenants = 0
    for webhook_id in delete_ids:
        touched_tenants += _remove_managed_webhook_id_from_tenants(
            session=session,
            webhook_id=webhook_id,
        )
    tenant_note = (
        f" Removed stale managed reference from {touched_tenants} tenant(s)."
        if touched_tenants > 0
        else ""
    )
    return len(delete_ids), f"Deleted {len(delete_ids)} conflicting Jira webhook URL subscription(s).{tenant_note}"


def _remove_managed_webhook_id_from_tenants(*, session: Session, webhook_id: int) -> int:
    removed_count = 0
    tenants = session.execute(select(Tenant)).scalars().all()
    for tenant in tenants:
        jira_config = tenant.jira_config
        if not isinstance(jira_config, dict):
            continue
        managed_ids = _parse_managed_webhook_ids(jira_config)
        if webhook_id not in managed_ids:
            continue
        updated_ids = [item for item in managed_ids if item != webhook_id]
        updated_config = dict(jira_config)
        updated_config["managed_webhook_ids"] = updated_ids
        tenant.jira_config = updated_config
        tenant.updated_at = datetime.now(timezone.utc)
        removed_count += 1
    return removed_count


def _with_preserved_jira_system_fields(*, existing: dict, proposed: dict) -> dict:
    merged = dict(proposed)
    for key in (
        "managed_webhook_ids",
        "webhook_last_provisioned_at",
        "webhook_last_error",
        "webhook_last_received_at",
        "webhook_last_delivery_id",
        "webhook_last_issue_key",
    ):
        if key in existing:
            merged[key] = existing.get(key)
    return merged


def _parse_discord_allowlist_requests(
    discord_config: dict | None,
    *,
    project_id: str | None = None,
) -> list[DiscordAllowlistRequestRead]:
    if not isinstance(discord_config, dict):
        return []
    raw = discord_config.get("allowlist_requests")
    if not isinstance(raw, list):
        return []
    normalized: list[DiscordAllowlistRequestRead] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        user_id = str(item.get("user_id") or "").strip()
        if not user_id:
            continue
        permissions_raw = item.get("permissions")
        permissions = (
            [str(value).strip() for value in permissions_raw if str(value).strip()]
            if isinstance(permissions_raw, list)
            else []
        )
        normalized.append(
            DiscordAllowlistRequestRead(
                project_id=project_id,
                user_id=user_id,
                requested_at=str(item.get("requested_at") or "").strip() or datetime.now(timezone.utc).isoformat(),
                channel_id=str(item.get("channel_id") or "").strip() or None,
                reason=str(item.get("reason") or "").strip() or None,
                permissions=permissions,
            )
        )
    normalized.sort(key=lambda item: item.requested_at, reverse=True)
    return normalized


def _notify_discord_allowlist_approved(
    *,
    session: Session,
    settings,
    tenant_id: str,
    user_id: str,
) -> bool:  # noqa: ANN001
    bot_token_ref = settings.discord_bot_token_secret_ref.strip()
    if not bot_token_ref:
        return False
    bot_token = resolve_scoped_secret_ref(
        session,
        secret_ref=bot_token_ref,
        encryption_key=settings.secrets_encryption_key,
        tenant_id=tenant_id,
    )
    if not bot_token:
        return False
    client = DiscordApiClient(bot_token=bot_token)
    client.send_direct_message(
        user_id=user_id,
        content="Your allowlist request has been approved. You can now run sensitive commands for this tenant.",
    )
    return True


def _delete_jira_webhooks(
    *,
    session: Session,
    tenant: Tenant,
    settings,  # noqa: ANN001
) -> tuple[bool, str, list[int]]:
    jira_config = dict(tenant.jira_config)
    connection_id = jira_config.get("connection_id")
    if not isinstance(connection_id, str) or not connection_id:
        jira_config["managed_webhook_ids"] = []
        jira_config["webhook_last_error"] = None
        tenant.jira_config = jira_config
        tenant.updated_at = datetime.now(timezone.utc)
        session.commit()
        return True, "No Jira connection linked; cleared local webhook metadata.", []

    webhook_ids = _parse_managed_webhook_ids(jira_config)
    if not webhook_ids:
        jira_config["webhook_last_error"] = None
        tenant.jira_config = jira_config
        tenant.updated_at = datetime.now(timezone.utc)
        session.commit()
        return True, "No managed Jira webhook IDs stored; nothing to delete.", []

    connection = session.get(JiraOAuthConnection, connection_id)
    if connection is None:
        jira_config["managed_webhook_ids"] = []
        jira_config["webhook_last_error"] = "Configured Jira connection was not found during webhook deletion"
        tenant.jira_config = jira_config
        tenant.updated_at = datetime.now(timezone.utc)
        session.commit()
        return False, "Configured Jira connection was not found.", webhook_ids

    try:
        access_token = _refresh_jira_connection_tokens(
            session,
            connection=connection,
            settings=settings,
            tenant_id=tenant.tenant_id,
        )
        client = _jira_oauth_client(session=session, settings=settings, tenant_id=tenant.tenant_id)
        client.delete_webhooks(
            access_token=access_token,
            cloud_id=connection.cloud_id,
            webhook_ids=webhook_ids,
        )
    except (ValueError, JiraOAuthError) as exc:
        error_message = f"Failed to delete Jira webhooks: {exc}"
        jira_config["webhook_last_error"] = error_message
        tenant.jira_config = jira_config
        tenant.updated_at = datetime.now(timezone.utc)
        session.commit()
        return False, error_message, webhook_ids

    jira_config["managed_webhook_ids"] = []
    jira_config["webhook_last_error"] = None
    tenant.jira_config = jira_config
    tenant.updated_at = datetime.now(timezone.utc)
    session.commit()
    return True, f"Deleted {len(webhook_ids)} Jira webhook(s).", webhook_ids


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
    if return_to == "edit" and not tenant_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="tenant_id is required when return_to=edit",
        )
    if tenant_id and session.get(Tenant, tenant_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    settings = get_settings()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)
    state_token = create_jira_oauth_state_token(
        exp=expires_at,
        secret=settings.jira_oauth_state_secret,
        return_to=return_to,
        tenant_id=tenant_id,
    )
    try:
        client = _jira_oauth_client(session=session, settings=settings, tenant_id=tenant_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    authorize_url = client.build_authorize_url(state=state_token)
    return JiraConnectStart(authorize_url=authorize_url, expires_at=expires_at)


@router.get("/jira/connect/callback", include_in_schema=False)
def jira_connect_callback(
    code: str = Query(..., min_length=1),
    state_token: str = Query(..., alias="state"),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    settings = get_settings()
    try:
        state = parse_jira_oauth_state_token(
            token=state_token,
            secret=settings.jira_oauth_state_secret,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    try:
        client = _jira_oauth_client(session=session, settings=settings, tenant_id=state.tenant_id)
        token_set = client.exchange_code(code=code)
        resources = client.list_accessible_resources(access_token=token_set.access_token)
    except (ValueError, JiraOAuthError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    if not resources:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No Jira resources were granted by OAuth",
        )
    resource = resources[0]
    now = datetime.now(timezone.utc)
    connection = JiraOAuthConnection(
        connection_id=str(uuid4()),
        account_id="unknown",
        account_email=None,
        cloud_id=resource.cloud_id,
        site_url=resource.site_url,
        scopes=token_set.scopes,
        access_token_encrypted=encrypt_value(
            plaintext=token_set.access_token,
            encryption_key=settings.secrets_encryption_key,
        ),
        refresh_token_encrypted=encrypt_value(
            plaintext=token_set.refresh_token,
            encryption_key=settings.secrets_encryption_key,
        ),
        access_token_expires_at=token_set.expires_at,
        created_at=now,
        updated_at=now,
    )
    session.add(connection)
    session.flush()

    if state.return_to == "edit" and state.tenant_id:
        tenant = session.get(Tenant, state.tenant_id)
        if tenant is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
        jira_config = dict(tenant.jira_config)
        jira_config["connection_id"] = connection.connection_id
        tenant.jira_config = jira_config
        tenant.updated_at = now

    session.commit()

    if state.return_to == "edit" and state.tenant_id:
        redirect_url = (
            f"{settings.admin_ui_base_url.rstrip('/')}/tenants/{quote(state.tenant_id, safe='')}/edit"
            f"?jira_oauth=success&jira_connection_id={quote(connection.connection_id, safe='')}"
        )
    else:
        redirect_url = (
            f"{settings.admin_ui_base_url.rstrip('/')}/tenants/new"
            f"?jira_oauth=success&jira_connection_id={quote(connection.connection_id, safe='')}"
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
    raw_report = jira_config.get("release_bootstrap")
    if not isinstance(raw_report, dict):
        return None
    raw_checked_at = raw_report.get("checked_at")
    checked_at = str(raw_checked_at).strip() if isinstance(raw_checked_at, str) and str(raw_checked_at).strip() else ""
    if not checked_at:
        return None
    raw_checks = raw_report.get("checks")
    checks = (
        {str(key): bool(value) for key, value in raw_checks.items()}
        if isinstance(raw_checks, dict)
        else {}
    )
    raw_details = raw_report.get("details")
    details = [str(item) for item in raw_details] if isinstance(raw_details, list) else []
    return ReleaseBootstrapReportRead(
        tenant_id=tenant_id,
        ok=bool(raw_report.get("ok")),
        checks=checks,
        details=details,
        checked_at=checked_at,
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

    checks: dict[str, bool] = {
        "jira_connection": False,
        "jira_project_keys": False,
        "jira_required_statuses": False,
        "github_installation": False,
    }
    details: list[str] = []

    jira_config = dict(tenant.jira_config or {})
    project_keys = jira_config.get("project_keys")
    if isinstance(project_keys, list):
        normalized_project_keys = [str(item).strip() for item in project_keys if str(item).strip()]
    else:
        normalized_project_keys = []
    checks["jira_project_keys"] = bool(normalized_project_keys)
    if not normalized_project_keys:
        details.append("Missing Jira project keys.")

    connection_id = jira_config.get("connection_id")
    if not isinstance(connection_id, str) or not connection_id:
        details.append("Jira OAuth connection is not linked.")
        connection = None
    else:
        connection = session.get(JiraOAuthConnection, connection_id)
        if connection is None:
            details.append("Configured Jira OAuth connection was not found.")
    checks["jira_connection"] = connection is not None

    if connection is not None and normalized_project_keys:
        settings = get_settings()
        try:
            access_token = _refresh_jira_connection_tokens(
                session,
                connection=connection,
                settings=settings,
                tenant_id=tenant_id,
            )
            client = _jira_oauth_client(session=session, settings=settings, tenant_id=tenant_id)
            quoted_projects = ", ".join(f"\"{key}\"" for key in normalized_project_keys)
            for required_status in RELEASE_BOOTSTRAP_REQUIRED_STATUSES:
                jql = (
                    f"project in ({quoted_projects}) AND status = \"{required_status}\" "
                    "ORDER BY updated DESC"
                )
                client.search_issues_by_jql(
                    access_token=access_token,
                    cloud_id=connection.cloud_id,
                    jql=jql,
                    max_results=1,
                )
            checks["jira_required_statuses"] = True
        except (ValueError, JiraOAuthError) as exc:
            details.append(
                "Jira required status validation failed "
                f"for {', '.join(RELEASE_BOOTSTRAP_REQUIRED_STATUSES)}: {exc}"
            )
            checks["jira_required_statuses"] = False

    github_installation_id = str(tenant.github_config.get("installation_id") or "").strip()
    checks["github_installation"] = bool(github_installation_id)
    if not github_installation_id:
        details.append("GitHub App installation is not connected.")

    ok = all(checks.values())
    if ok:
        details.append("Release bootstrap checks passed.")

    checked_at = datetime.now(timezone.utc).isoformat()
    jira_config["release_bootstrap"] = {
        "ok": ok,
        "checks": checks,
        "details": details,
        "checked_at": checked_at,
    }
    tenant.jira_config = jira_config
    tenant.updated_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(tenant)

    return ReleaseBootstrapReportRead(
        tenant_id=tenant_id,
        ok=ok,
        checks=checks,
        details=details,
        checked_at=checked_at,
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
