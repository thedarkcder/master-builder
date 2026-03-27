from __future__ import annotations

import logging
import secrets
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from orchestrator.api.dependencies import get_session
from orchestrator.api.discord.shared.state import command_matches
from orchestrator.api.transport_runtime import execute_http_ingress_result, http_json_response_action
from orchestrator.api.schemas import DiscordCommandRequest
from orchestrator.core.followup_context_service import resolve_followup_context
from orchestrator.api.webhooks.payload_utils import (
    extract_webhook_token as _extract_webhook_token,
    read_json_payload as _read_json_payload,
)
from orchestrator.core.communications import IngressResult, TransportEnvelope
from orchestrator.core.config import get_settings
from orchestrator.core.tenant_secret_service import resolve_scoped_secret_ref
from orchestrator.core.webhook_job_queue import (
    WEBHOOK_TRANSPORT_DISCORD_COMMAND,
    WebhookJobEnqueueRequest,
    enqueue_webhook_job,
)
from orchestrator.storage.models import Tenant
from orchestrator.storage.run_queue_events import notify_webhook_job_enqueued

router = APIRouter(tags=["discord-webhook"])
logger = logging.getLogger(__name__)


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
    subject_key = _resolve_discord_command_subject_key(
        session=session,
        tenant_id=tenant_id,
        channel_id=command_payload.channel_id,
        user_id=command_payload.user_id,
    )
    enqueue_result = enqueue_webhook_job(
        session,
        request=WebhookJobEnqueueRequest(
            transport=WEBHOOK_TRANSPORT_DISCORD_COMMAND,
            request_id=request_id,
            tenant_id=tenant_id,
            project_id=None,
            subject_key=subject_key,
            dedupe_key=request_id,
            event_type=envelope.event_type,
            payload_json=command_payload.model_dump(mode="json"),
            context_json={
                "defer_seed_issues": command_matches(
                    normalized_command,
                    command_name="issues",
                    subcommand="seed",
                ),
            },
        ),
    )
    notify_webhook_job_enqueued(
        session,
        transport=WEBHOOK_TRANSPORT_DISCORD_COMMAND,
        tenant_id=tenant_id,
        project_id=None,
        subject_key=subject_key,
        job_id=enqueue_result.job.job_id,
        dedupe_key=enqueue_result.job.dedupe_key,
    )
    session.commit()
    return IngressResult(
        actions=(
            http_json_response_action(
                status_code=status.HTTP_200_OK,
                content={
                    "request_id": request_id,
                    "tenant_id": tenant_id,
                    "accepted": True,
                    "deferred": True,
                    "queued": enqueue_result.created,
                    "job_id": enqueue_result.job.job_id,
                },
            ),
        ),
    )


@router.post("/discord/webhook/{tenant_id}")
async def ingest_discord_webhook(
    tenant_id: str,
    request: Request,
    session: Session = Depends(get_session),
) -> object:
    request_id = request.headers.get("X-Request-Id") or str(uuid4())
    envelope = TransportEnvelope(
        transport="discord_webhook",
        event_type="command_webhook",
        request_id=request_id,
        tenant_id_hint=tenant_id,
    )
    result = await build_discord_webhook_ingress_result(
        tenant_id=tenant_id,
        request=request,
        session=session,
        request_id=request_id,
        envelope=envelope,
    )
    return execute_http_ingress_result(result=result, envelope=envelope)


def _resolve_discord_command_subject_key(
    *,
    session: Session,
    tenant_id: str,
    channel_id: str | None,
    user_id: str,
) -> str:
    if channel_id:
        context = resolve_followup_context(
            session=session,
            tenant_id=tenant_id,
            channel_id=channel_id,
        )
        if context is not None:
            context_id = str(getattr(context, "context_id", "") or "").strip()
            if context_id:
                return f"discord_followup:{context_id}"
        return f"discord_channel:{tenant_id}:{channel_id}"
    return f"discord_user:{tenant_id}:{user_id}"
