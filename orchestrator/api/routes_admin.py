from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from orchestrator.api.dependencies import get_session
from orchestrator.api.schemas import (
    GitHubRepositoryRead,
    GitHubInstallStart,
    IntegrationTestResult,
    JiraConnectStart,
    JiraProjectRead,
    RunRead,
    TenantCreate,
    TenantRead,
    TenantUpdate,
)
from orchestrator.core.config import get_settings
from orchestrator.core.jira_oauth_state import (
    create_jira_oauth_state_token,
    parse_jira_oauth_state_token,
)
from orchestrator.core.secrets import decrypt_value, encrypt_value
from orchestrator.core.github_install_state import create_install_state_token, parse_install_state_token
from orchestrator.core.security import require_admin
from orchestrator.storage.models import JiraOAuthConnection, Run, Tenant
from orchestrator.tools.github_app import GitHubApiError, github_client_from_tenant_config
from orchestrator.tools.jira_oauth import JiraOAuthClient, JiraOAuthClientConfig, JiraOAuthError

router = APIRouter(prefix="/api/admin", tags=["admin"])


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
    settings = get_settings()
    github_config = dict(raw_github_config)
    github_config["app_id_ref"] = settings.github_app_id_ref
    github_config["private_key_ref"] = settings.github_private_key_ref
    return github_config


def _resolve_secret_ref(ref_name: str) -> str:
    value = os.environ.get(ref_name)
    if not value:
        raise ValueError(f"Missing secret value for ref '{ref_name}'")
    return value


def _jira_oauth_client(*, settings) -> JiraOAuthClient:  # noqa: ANN001
    client_id = _resolve_secret_ref(settings.jira_oauth_client_id_ref)
    client_secret = _resolve_secret_ref(settings.jira_oauth_client_secret_ref)
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

    client = _jira_oauth_client(settings=settings)
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
        client = _jira_oauth_client(settings=settings)
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
        client = _jira_oauth_client(settings=settings)
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
    client = _jira_oauth_client(settings=settings)
    projects = client.list_projects(access_token=access_token, cloud_id=connection.cloud_id)
    return [JiraProjectRead(key=project.key, name=project.name) for project in projects]


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
    tenant_id = _allocate_tenant_id(session, name=payload.name)
    now = datetime.now(timezone.utc)
    tenant = Tenant(
        tenant_id=tenant_id,
        name=payload.name,
        is_enabled=payload.is_enabled,
        jira_config=payload.jira.model_dump(),
        github_config=_with_managed_github_refs(payload.github.model_dump()),
        repos_config=payload.repos.model_dump(),
        policy_config=payload.policy.model_dump(),
        discord_config=payload.discord.model_dump() if payload.discord else None,
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
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    tenant.name = payload.name
    tenant.is_enabled = payload.is_enabled
    tenant.jira_config = payload.jira.model_dump()
    tenant.github_config = _with_managed_github_refs(payload.github.model_dump())
    tenant.repos_config = payload.repos.model_dump()
    tenant.policy_config = payload.policy.model_dump()
    tenant.discord_config = payload.discord.model_dump() if payload.discord else None
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
        client = _jira_oauth_client(settings=settings)
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
        github_client_from_tenant_config(_with_managed_github_refs(github))
    except ValueError as exc:
        return IntegrationTestResult(ok=False, details=str(exc))

    return IntegrationTestResult(
        ok=True,
        details="GitHub tenant configuration looks valid and secret refs resolve",
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
        client = github_client_from_tenant_config(_with_managed_github_refs(github))
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
