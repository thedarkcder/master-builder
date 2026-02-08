from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from orchestrator.api.dependencies import get_session
from orchestrator.api.schemas import (
    AdminLoginRequest,
    AdminLoginResponse,
    AdminSessionRead,
    GitHubRepositoryRead,
    GitHubInstallStart,
    IntegrationTestResult,
    JiraConnectStart,
    ReadyGatePreviewRead,
    ReadyIssuePreviewRead,
    JiraProjectRead,
    ManagedSecretRead,
    ManagedSecretResolveRequest,
    ManagedSecretResolveResult,
    ManagedSecretUpsert,
    RepoBootstrapStateRead,
    RunRead,
    TenantCreate,
    TenantRead,
    TenantUpdate,
)
from orchestrator.core.config import get_settings
from orchestrator.core.enforcement_context import EnforcementAssetsError, validate_enforcement_assets
from orchestrator.core.secret_manager import (
    list_managed_secret_refs,
    normalize_secret_ref,
    resolve_secret_ref,
    resolve_secret_ref_metadata,
    upsert_managed_secret,
)
from orchestrator.core.jira_oauth_state import (
    create_jira_oauth_state_token,
    parse_jira_oauth_state_token,
)
from orchestrator.core.secrets import decrypt_value, encrypt_value
from orchestrator.core.github_install_state import create_install_state_token, parse_install_state_token
from orchestrator.core.admin_tokens import create_admin_access_token
from orchestrator.core.security import require_admin, validate_admin_credentials
from orchestrator.storage.models import JiraOAuthConnection, ManagedSecret, Run, Tenant
from orchestrator.tools.github_app import GitHubApiError, github_client_from_tenant_config
from orchestrator.tools.jira_oauth import JiraOAuthClient, JiraOAuthClientConfig, JiraOAuthError
from orchestrator.tools.discord_api import DiscordApiClient, DiscordApiError
from orchestrator.tools.bootstrap import list_repo_bootstrap_states

router = APIRouter(prefix="/api/admin", tags=["admin"])
GITHUB_APP_SLUG_SECRET_REF = "GITHUB_APP_SLUG"
GITHUB_APP_ID_SECRET_REF = "GITHUB_APP_ID"
GITHUB_APP_PRIVATE_KEY_SECRET_REF = "GITHUB_APP_PRIVATE_KEY"
JIRA_OAUTH_CLIENT_ID_SECRET_REF = "JIRA_OAUTH_CLIENT_ID"
JIRA_OAUTH_CLIENT_SECRET_SECRET_REF = "JIRA_OAUTH_CLIENT_SECRET"


@router.post("/auth/login", response_model=AdminLoginResponse)
def admin_login(payload: AdminLoginRequest) -> AdminLoginResponse:
    if not validate_admin_credentials(username=payload.username, password=payload.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin credentials",
            headers={"WWW-Authenticate": "Basic"},
        )

    settings = get_settings()
    access_token, expires_in = create_admin_access_token(
        username=payload.username,
        secret=settings.admin_token_secret,
        ttl_seconds=settings.admin_token_ttl_seconds,
    )
    return AdminLoginResponse(access_token=access_token, expires_in=expires_in)


@router.get("/auth/me", response_model=AdminSessionRead)
def admin_me(admin_username: str = Depends(require_admin)) -> AdminSessionRead:
    return AdminSessionRead(username=admin_username)


def _tenant_to_schema(tenant: Tenant) -> TenantRead:
    repos_config = _normalize_repos_config(tenant.repos_config)
    return TenantRead(
        tenant_id=tenant.tenant_id,
        name=tenant.name,
        is_enabled=tenant.is_enabled,
        jira=tenant.jira_config,
        github=tenant.github_config,
        repos=repos_config,
        policy=tenant.policy_config,
        discord=tenant.discord_config,
        created_at=tenant.created_at,
        updated_at=tenant.updated_at,
    )


def _run_to_schema(run: Run) -> RunRead:
    return RunRead(
        run_id=run.run_id,
        tenant_id=run.tenant_id,
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


def _with_managed_github_refs(raw_github_config: dict) -> dict:
    github_config = dict(raw_github_config)
    github_config["app_id_ref"] = GITHUB_APP_ID_SECRET_REF
    github_config["private_key_ref"] = GITHUB_APP_PRIVATE_KEY_SECRET_REF
    return github_config


def _normalize_repos_config(raw_repos_config: dict | None) -> dict:
    repos_config = dict(raw_repos_config or {})
    github_repository = str(repos_config.get("github_repository") or "").strip()
    return {"github_repository": github_repository or None}


def _build_repos_config(payload_repos: dict) -> dict:
    repos_config = _normalize_repos_config(payload_repos)
    if not repos_config["github_repository"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="github_repository is required",
        )
    return repos_config


def _render_discord_channel_name(*, template: str, tenant_id: str) -> str:
    try:
        rendered = template.format(tenant_id=tenant_id)
    except KeyError as exc:
        raise ValueError("Discord channel template may only reference {tenant_id}") from exc

    normalized = re.sub(r"[^a-z0-9-_]+", "-", rendered.strip().lower())
    normalized = re.sub(r"-{2,}", "-", normalized).strip("-")
    if not normalized:
        raise ValueError("Discord channel template produced an empty channel name")
    return normalized[:100]


def _resolve_discord_config(
    *,
    session: Session,
    tenant_id: str,
    payload_discord: dict | None,
    existing_discord: dict | None = None,
) -> dict | None:
    if payload_discord is None:
        return None

    settings = get_settings()
    discord_config = dict(payload_discord)
    configured_channel_id = str(discord_config.get("channel_id") or "").strip()
    if not configured_channel_id and isinstance(existing_discord, dict):
        configured_channel_id = str(existing_discord.get("channel_id") or "").strip()

    if configured_channel_id:
        discord_config["channel_id"] = configured_channel_id
        return discord_config

    guild_id = settings.discord_guild_id.strip()
    if not guild_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="ORCHESTRATOR_DISCORD_GUILD_ID is required when Discord is enabled",
        )

    token_ref = settings.discord_bot_token_secret_ref.strip()
    if not token_ref:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="ORCHESTRATOR_DISCORD_BOT_TOKEN_SECRET_REF must be configured",
        )
    bot_token = resolve_secret_ref(
        session,
        secret_ref=token_ref,
        encryption_key=settings.secrets_encryption_key,
    )
    if not bot_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Missing Discord bot token secret '{token_ref}'",
        )

    try:
        channel_name = _render_discord_channel_name(
            template=settings.discord_channel_name_template,
            tenant_id=tenant_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    category_id = settings.discord_channel_category_id.strip() or None
    client = DiscordApiClient(bot_token=bot_token)
    try:
        channel = client.ensure_text_channel(
            guild_id=guild_id,
            name=channel_name,
            parent_id=category_id,
        )
    except DiscordApiError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to provision Discord channel: {exc}",
        ) from exc
    discord_config["channel_id"] = channel.channel_id
    return discord_config


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


def _resolve_secret_ref(session: Session, *, ref_name: str, settings) -> str:  # noqa: ANN001
    value = resolve_secret_ref(
        session,
        secret_ref=ref_name,
        encryption_key=settings.secrets_encryption_key,
    )
    if not value:
        raise ValueError(f"Missing secret value for ref '{ref_name}'")
    return value


def _jira_oauth_client(*, session: Session, settings) -> JiraOAuthClient:  # noqa: ANN001
    client_id = _resolve_secret_ref(session, ref_name=JIRA_OAUTH_CLIENT_ID_SECRET_REF, settings=settings)
    client_secret = _resolve_secret_ref(session, ref_name=JIRA_OAUTH_CLIENT_SECRET_SECRET_REF, settings=settings)
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
) -> str:  # noqa: ANN001
    now = datetime.now(timezone.utc)
    if connection.access_token_expires_at - now > timedelta(seconds=60):
        return decrypt_value(
            ciphertext=connection.access_token_encrypted,
            encryption_key=settings.secrets_encryption_key,
        )

    client = _jira_oauth_client(session=session, settings=settings)
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


def _secret_metadata_to_schema(metadata) -> ManagedSecretRead:  # noqa: ANN001
    return ManagedSecretRead(
        secret_ref=metadata.secret_ref,
        source=metadata.source,
        updated_at=metadata.updated_at,
    )


def _default_ready_jql(*, project_keys: list[str], ready_statuses: list[str]) -> str:
    quoted_projects = ", ".join(f"\"{key}\"" for key in project_keys)
    quoted_statuses = ", ".join(f"\"{status}\"" for status in ready_statuses)
    return f"project in ({quoted_projects}) AND status in ({quoted_statuses}) ORDER BY updated DESC"


@router.get("/secrets", response_model=list[ManagedSecretRead])
def list_secrets(
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> list[ManagedSecretRead]:
    refs = list_managed_secret_refs(session)
    return [_secret_metadata_to_schema(metadata) for metadata in refs]


@router.put("/secrets/{secret_ref:path}", response_model=ManagedSecretRead)
def upsert_secret(
    secret_ref: str,
    payload: ManagedSecretUpsert,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> ManagedSecretRead:
    settings = get_settings()
    try:
        metadata = upsert_managed_secret(
            session,
            secret_ref=secret_ref,
            plaintext_value=payload.value,
            encryption_key=settings.secrets_encryption_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return _secret_metadata_to_schema(metadata)


@router.delete("/secrets/{secret_ref:path}", status_code=status.HTTP_204_NO_CONTENT)
def delete_secret(
    secret_ref: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> Response:
    try:
        normalized_ref = normalize_secret_ref(secret_ref)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    row = session.get(ManagedSecret, normalized_ref)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Secret ref not found")

    session.delete(row)
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/secrets/resolve", response_model=ManagedSecretResolveResult)
def resolve_secret(
    payload: ManagedSecretResolveRequest,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> ManagedSecretResolveResult:
    settings = get_settings()
    try:
        secret_ref = normalize_secret_ref(payload.secret_ref)
        metadata = resolve_secret_ref_metadata(session, secret_ref=secret_ref)
        resolved_value = resolve_secret_ref(
            session,
            secret_ref=secret_ref,
            encryption_key=settings.secrets_encryption_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return ManagedSecretResolveResult(
        secret_ref=secret_ref,
        source=metadata.source,
        resolved=bool(resolved_value),
    )


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
        client = _jira_oauth_client(session=session, settings=settings)
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
        client = _jira_oauth_client(session=session, settings=settings)
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
            f"{settings.admin_ui_base_url.rstrip('/')}/tenants/new/jira"
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
        )
        client = _jira_oauth_client(session=session, settings=settings)
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
    discord_config = _resolve_discord_config(
        session=session,
        tenant_id=tenant_id,
        payload_discord=payload.discord.model_dump() if payload.discord else None,
    )

    tenant = Tenant(
        tenant_id=tenant_id,
        name=payload.name,
        is_enabled=payload.is_enabled,
        jira_config=payload.jira.model_dump(),
        github_config=_with_managed_github_refs(payload.github.model_dump()),
        repos_config=_build_repos_config(payload.repos.model_dump()),
        policy_config=payload.policy.model_dump(),
        discord_config=discord_config,
        created_at=now,
        updated_at=now,
    )
    session.add(tenant)
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
    tenant.jira_config = payload.jira.model_dump()
    tenant.github_config = _with_managed_github_refs(payload.github.model_dump())
    tenant.repos_config = _build_repos_config(payload.repos.model_dump())
    tenant.policy_config = payload.policy.model_dump()
    tenant.discord_config = _resolve_discord_config(
        session=session,
        tenant_id=tenant_id,
        payload_discord=payload.discord.model_dump() if payload.discord else None,
        existing_discord=tenant.discord_config,
    )
    tenant.updated_at = datetime.now(timezone.utc)

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
        )
        client = _jira_oauth_client(session=session, settings=settings)
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
        client = github_client_from_tenant_config(
            _with_managed_github_refs(github),
            secret_lookup=lambda ref: resolve_secret_ref(
                session,
                secret_ref=ref,
                encryption_key=settings.secrets_encryption_key,
            ),
        )
        repositories = client.list_installation_repositories()
    except ValueError as exc:
        return IntegrationTestResult(ok=False, details=str(exc))
    except GitHubApiError as exc:
        return IntegrationTestResult(ok=False, details=f"GitHub API validation failed: {exc}")

    if not repositories:
        return IntegrationTestResult(
            ok=False,
            details="GitHub App is connected, but no repositories are accessible for this installation",
        )

    return IntegrationTestResult(
        ok=True,
        details=f"GitHub App config is valid; {len(repositories)} repository/repositories accessible",
    )


@router.post("/tenants/{tenant_id}/jira/disconnect", response_model=TenantRead)
def disconnect_jira_connection(
    tenant_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> TenantRead:
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    jira_config = dict(tenant.jira_config)
    jira_config["connection_id"] = None
    tenant.jira_config = jira_config
    tenant.updated_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(tenant)
    return _tenant_to_schema(tenant)


@router.post("/tenants/{tenant_id}/github/disconnect", response_model=TenantRead)
def disconnect_github_installation(
    tenant_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> TenantRead:
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    github_config = dict(tenant.github_config)
    github_config["installation_id"] = None
    github_config.pop("installation_setup_action", None)
    github_config.pop("installation_updated_at", None)
    tenant.github_config = github_config
    tenant.updated_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(tenant)
    return _tenant_to_schema(tenant)


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
    try:
        app_slug = _resolve_secret_ref(
            session,
            ref_name=GITHUB_APP_SLUG_SECRET_REF,
            settings=settings,
        ).strip()
    except ValueError:
        app_slug = ""
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
            f"{settings.admin_ui_base_url.rstrip('/')}/tenants/new/github"
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
            secret_lookup=lambda ref: resolve_secret_ref(
                session,
                secret_ref=ref,
                encryption_key=settings.secrets_encryption_key,
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
    status_filter: str | None = Query(default=None, alias="status"),
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> list[RunRead]:
    query = select(Run).order_by(Run.created_at.desc())

    if tenant_id:
        query = query.where(Run.tenant_id == tenant_id)
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
