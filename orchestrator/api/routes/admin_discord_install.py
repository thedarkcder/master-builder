from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from orchestrator.api.dependencies import get_session
from orchestrator.api.schemas import DiscordInstallStart
from orchestrator.core.config import get_settings
from orchestrator.core.discord_install_state import (
    create_discord_install_state_token,
    parse_discord_install_state_token,
)
from orchestrator.core.security import (
    AuthenticatedPrincipal,
    require_admin,
    require_authenticated_principal,
    require_tenant_permission,
)
from orchestrator.core.tenant_access import PERMISSION_WORKSPACE_MANAGE
from orchestrator.storage.models import Tenant

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.post("/tenants/{tenant_id}/discord/install/start", response_model=DiscordInstallStart)
def start_discord_install(
    tenant_id: str,
    return_to: str = Query(default="edit", pattern="^(edit|wizard)$"),
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> DiscordInstallStart:
    settings = get_settings()
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    if principal.is_platform_super_admin:
        require_admin(principal=principal)
    else:
        require_tenant_permission(principal=principal, tenant_id=tenant_id, permission_key=PERMISSION_WORKSPACE_MANAGE)

    client_id = settings.discord_oauth_client_id.strip()
    if not client_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Discord application client id is not configured")

    expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)
    state_token = create_discord_install_state_token(
        tenant_id=tenant_id,
        exp=expires_at,
        secret=settings.discord_install_state_secret,
        return_to=return_to,
        installer_user_id=principal.user_id,
    )
    redirect_uri = f"{settings.public_api_base_url.rstrip('/')}/api/admin/discord/install/callback"
    existing_guild_id = str((tenant.discord_config or {}).get("guild_id") or "").strip()
    query: dict[str, str] = {
        "client_id": client_id,
        "scope": "bot applications.commands",
        "permissions": str(int(settings.discord_bot_permissions)),
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "state": state_token,
        "disable_guild_select": "false",
    }
    if existing_guild_id:
        query["guild_id"] = existing_guild_id
    install_url = f"https://discord.com/oauth2/authorize?{urlencode(query)}"
    return DiscordInstallStart(install_url=install_url, expires_at=expires_at)


@router.get("/discord/install/callback", include_in_schema=False)
def discord_install_callback(
    state_token: str = Query(..., alias="state"),
    guild_id: str = Query(..., min_length=1),
    code: str | None = Query(default=None),
    permissions: str | None = Query(default=None),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    settings = get_settings()
    try:
        state = parse_discord_install_state_token(token=state_token, secret=settings.discord_install_state_secret)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    tenant = session.get(Tenant, state.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    discord_config = dict(tenant.discord_config or {})
    discord_config["guild_id"] = guild_id.strip()
    discord_config["installed_at"] = datetime.now(timezone.utc).isoformat()
    discord_config["installation_code_received"] = bool(code)
    if permissions:
        discord_config["installation_permissions"] = permissions
    if state.installer_user_id:
        discord_config["installer_user_id"] = state.installer_user_id
    tenant.discord_config = discord_config
    tenant.updated_at = datetime.now(timezone.utc)
    session.commit()

    if state.return_to == "wizard":
        redirect_url = (
            f"{settings.admin_ui_base_url.rstrip('/')}/tenants/new/discord"
            f"?tenant_id={quote(tenant.tenant_id, safe='')}&discord_install=success"
        )
    else:
        redirect_url = (
            f"{settings.admin_ui_base_url.rstrip('/')}/tenants/{quote(tenant.tenant_id, safe='')}/edit/discord"
            "?discord_install=success"
        )
    return RedirectResponse(url=redirect_url, status_code=status.HTTP_302_FOUND)
