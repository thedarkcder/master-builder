from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from orchestrator.api.dependencies import get_session
from orchestrator.api.commands.entrypoint import execute_tenant_discord_ingress_command
from orchestrator.api.discord.shared.state import command_matches
from orchestrator.api.transport_runtime import execute_http_ingress_result, http_json_response_action
from orchestrator.api.schemas import DiscordCommandRequest
from orchestrator.core.error_observability import emit_hard_error
from orchestrator.api.webhooks.payload_utils import (
    extract_webhook_token as _extract_webhook_token,
    read_json_payload as _read_json_payload,
)
from orchestrator.core.communications import DeferredTransportWork, IngressResult, TransportEnvelope
from orchestrator.core.config import get_settings
from orchestrator.core.tenant_secret_service import resolve_scoped_secret_ref
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import Tenant

router = APIRouter(tags=["discord-webhook"])
logger = logging.getLogger(__name__)

execute_discord_ingress_command = execute_tenant_discord_ingress_command


def _discord_webhook_task_callback(
    *,
    tenant_id: str,
    user_id: str,
    command: str,
) -> Any:
    def _callback(task: asyncio.Task[Any]) -> None:
        if task.cancelled():
            logger.warning(
                "discord_webhook_deferred_command_cancelled tenant_id=%s user_id=%s command=%s",
                tenant_id,
                user_id,
                command,
            )
            return

        exc = task.exception()
        if exc is None:
            return
        error_ref = uuid4().hex[:8]
        logger.exception(
            "discord_webhook_deferred_command_failed tenant_id=%s user_id=%s command=%s error_ref=%s error=%s",
            tenant_id,
            user_id,
            command,
            error_ref,
            exc,
        )
        emit_hard_error(
            event="discord_webhook_deferred_command_failed",
            error_ref=error_ref,
            exc=exc,
            context={"tenant_id": tenant_id, "user_id": user_id, "command": command},
        )

    return _callback


async def _run_discord_webhook_command(
    *,
    tenant_id: str,
    payload: DiscordCommandRequest,
    defer_seed_issues: bool,
) -> None:
    session_factory = create_session_factory()
    session = session_factory()
    try:
        execute_discord_ingress_command(
            tenant_id=tenant_id,
            payload=payload,
            session=session,
            defer_seed_issues=defer_seed_issues,
            allow_plain_ask=True,
        )
    finally:
        session.close()


async def build_discord_webhook_ingress_result(
    *,
    tenant_id: str,
    request: Request,
    session: Session,
    request_id: str,
    envelope: TransportEnvelope,
) -> IngressResult:
    settings = get_settings()
    logger.info("discord_webhook_received request_id=%s tenant_id=%s", request_id, tenant_id)

    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown tenant")
    if not tenant.is_enabled:
        return IngressResult(
            actions=(
                http_json_response_action(
                    status_code=status.HTTP_200_OK,
                    content={
                        "request_id": request_id,
                        "tenant_id": tenant_id,
                        "accepted": False,
                        "reason": "tenant_disabled",
                    },
                ),
            )
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
        if not presented_token or not secrets.compare_digest(presented_token, expected_token):
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

    normalized_command = command.strip()
    command_payload = DiscordCommandRequest(
        user_id=user_id.strip(),
        command=normalized_command,
        channel_id=channel_id.strip() if isinstance(channel_id, str) and channel_id.strip() else None,
    )
    deferred = DeferredTransportWork(
        kind="discord_webhook_command",
        runner=lambda: _run_discord_webhook_command(
            tenant_id=tenant_id,
            payload=command_payload,
            defer_seed_issues=command_matches(normalized_command, command_name="issues", subcommand="seed"),
        ),
        metadata={
            "request_id": request_id,
            "tenant_id": tenant_id,
            "transport": envelope.transport,
            "event_type": envelope.event_type,
        },
        on_scheduled=lambda task: task.add_done_callback(
            _discord_webhook_task_callback(
                tenant_id=tenant_id,
                user_id=user_id,
                command=normalized_command,
            )
        ),
    )
    return IngressResult(
        actions=(
            http_json_response_action(
                status_code=status.HTTP_200_OK,
                content={
                    "request_id": request_id,
                    "tenant_id": tenant_id,
                    "accepted": True,
                    "deferred": True,
                },
            ),
        ),
        deferred_work=(deferred,),
    )


@router.post("/discord/webhook/{tenant_id}")
async def ingest_discord_webhook(
    tenant_id: str,
    request: Request,
    session: Session = Depends(get_session),
) -> object:
    request_id = request.headers.get("X-Request-Id") or str(uuid4())
    result = await build_discord_webhook_ingress_result(
        tenant_id=tenant_id,
        request=request,
        session=session,
        request_id=request_id,
        envelope=TransportEnvelope(
            transport="discord_webhook",
            event_type="command_webhook",
            request_id=request_id,
        ),
    )
    return execute_http_ingress_result(result=result, task_scheduler=asyncio.create_task)
