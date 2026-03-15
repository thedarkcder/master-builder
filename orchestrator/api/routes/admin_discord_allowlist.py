from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from orchestrator.api.admin import integration_dependencies as deps
from orchestrator.api.admin.route_helpers import parse_discord_allowlist_requests
from orchestrator.api.admin.tenant_actions import (
    approve_discord_allowlist_request as approve_discord_allowlist_request_impl,
    list_discord_allowlist_requests as list_discord_allowlist_requests_impl,
)
from orchestrator.api.dependencies import get_session
from orchestrator.api.schemas import DiscordAllowlistApprovalResult, DiscordAllowlistRequestRead
from orchestrator.core.config import get_settings
from orchestrator.core.security import require_admin

router = APIRouter(prefix="/api/admin", tags=["admin"])


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
    return list_discord_allowlist_requests_impl(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        parse_discord_allowlist_requests_fn=parse_discord_allowlist_requests,
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
    return approve_discord_allowlist_request_impl(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        user_id=user_id,
        settings=get_settings(),
        parse_discord_allowlist_requests_fn=parse_discord_allowlist_requests,
        notify_discord_allowlist_approved_fn=deps.notify_discord_allowlist_approved,
    )
