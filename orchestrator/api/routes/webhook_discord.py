from __future__ import annotations

import logging
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from orchestrator.api.dependencies import get_session
from orchestrator.api.commands.entrypoint import execute_tenant_discord_ingress_command
from orchestrator.api.discord.shared.state import command_matches
from orchestrator.api.schemas import DiscordCommandRequest
from orchestrator.api.webhooks.payload_utils import (
    extract_webhook_token as _extract_webhook_token,
    read_json_payload as _read_json_payload,
)
from orchestrator.core.config import get_settings
from orchestrator.core.secret_manager import resolve_scoped_secret_ref
from orchestrator.storage.models import Tenant

router = APIRouter(tags=["discord-webhook"])
logger = logging.getLogger(__name__)

execute_discord_ingress_command = execute_tenant_discord_ingress_command


@router.post("/discord/webhook/{tenant_id}")
async def ingest_discord_webhook(
    tenant_id: str,
    request: Request,
    session: Session = Depends(get_session),
) -> JSONResponse:
    settings = get_settings()
    request_id = request.headers.get("X-Request-Id") or str(uuid4())
    logger.info("discord_webhook_received request_id=%s tenant_id=%s", request_id, tenant_id)

    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown tenant")
    if not tenant.is_enabled:
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "request_id": request_id,
                "tenant_id": tenant_id,
                "accepted": False,
                "reason": "tenant_disabled",
            },
        )

    discord_config = tenant.discord_config or {}
    command_secret_ref = str(discord_config.get("command_secret_ref") or "").strip()
    if command_secret_ref:
        presented_token = _extract_webhook_token(request)
        expected_token = resolve_scoped_secret_ref(
            session,
            secret_ref=command_secret_ref,
            encryption_key=settings.secrets_encryption_key,
            tenant_id=tenant_id,
        )
        if not expected_token:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Discord command authentication is misconfigured",
            )
        if not presented_token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid Discord webhook token",
            )
        import secrets

        if not secrets.compare_digest(presented_token, expected_token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid Discord webhook token",
            )

    payload, _ = await _read_json_payload(request, request_id=request_id, source="discord")
    user_id = payload.get("user_id")
    command = payload.get("command")
    channel_id = payload.get("channel_id")
    if not isinstance(user_id, str) or not user_id.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing user_id")
    if not isinstance(command, str) or not command.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing command")
    if channel_id is not None and not isinstance(channel_id, str):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid channel_id")

    command_response = execute_discord_ingress_command(
        tenant_id=tenant_id,
        payload=DiscordCommandRequest(
            user_id=user_id.strip(),
            command=command.strip(),
            channel_id=channel_id.strip() if isinstance(channel_id, str) and channel_id.strip() else None,
        ),
        session=session,
        defer_seed_issues=command_matches(command, command_name="issues", subcommand="seed"),
        allow_plain_ask=True,
    )
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "request_id": request_id,
            "tenant_id": tenant_id,
            "accepted": True,
            "result": command_response.model_dump(),
        },
    )
