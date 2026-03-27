from __future__ import annotations

import logging
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from orchestrator.api.dependencies import get_session
from orchestrator.api.discord.interactions.application import (
    build_default_discord_interaction_dispatch_deps,
    build_discord_interaction_ingress_result,
)
from orchestrator.api.discord.interactions.auth import (
    _resolve_discord_interactions_public_key,
    _validate_discord_interaction_signature,
)
from orchestrator.api.webhooks.payload_utils import read_json_payload as _read_json_payload
from orchestrator.api.transport_runtime import execute_http_ingress_result
from orchestrator.core.communications import (
    DiscordInteractionResponseAction,
    HttpJsonResponseBytesAction,
    IngressResult,
    TransportAction,
    TransportEnvelope,
)
from orchestrator.core.config import get_settings
from orchestrator.core.followup_context_service import resolve_followup_context
from orchestrator.core.webhook_job_queue import (
    WEBHOOK_TRANSPORT_DISCORD_INTERACTION,
    WebhookJobEnqueueRequest,
    enqueue_webhook_job,
)
from orchestrator.storage.run_queue_events import notify_webhook_job_enqueued

router = APIRouter(tags=["discord-interactions"])
logger = logging.getLogger(__name__)

def _http_result_from_interaction_result(result: IngressResult) -> IngressResult:
    http_actions: list[TransportAction] = []
    for action in result.actions:
        if not isinstance(action, DiscordInteractionResponseAction):
            http_actions.append(action)
            continue
        http_actions.append(
            HttpJsonResponseBytesAction(
                status_code=int(action.status_code),
                body=action.body,
                headers={"content-type": "application/json"},
            )
        )
    return IngressResult(actions=tuple(http_actions), deferred_work=())


@router.post("/discord/interactions")
async def ingest_discord_interaction(
    request: Request,
    session: Session = Depends(get_session),
) -> object:
    settings = get_settings()
    request_id = request.headers.get("X-Request-Id") or str(uuid4())

    payload, payload_bytes = await _read_json_payload(request, request_id=request_id, source="discord")
    public_key = _resolve_discord_interactions_public_key(session=session, settings=settings)
    _validate_discord_interaction_signature(
        request=request,
        payload_bytes=payload_bytes,
        public_key=public_key,
    )

    result = await build_discord_interaction_ingress_result(
        payload=payload,
        session=session,
        envelope=TransportEnvelope(
            transport="discord_http",
            event_type="interaction_create",
            request_id=request_id,
            payload=payload,
        ),
        dispatch_deps=build_default_discord_interaction_dispatch_deps(
            task_scheduler=lambda coro: coro,
            logger=logger,
        ),
    )
    if result.deferred_work:
        tenant_id, project_id, subject_key = _resolve_interaction_subject_scope(
            session=session,
            payload=payload,
        )
        enqueue_result = enqueue_webhook_job(
            session,
            request=WebhookJobEnqueueRequest(
                transport=WEBHOOK_TRANSPORT_DISCORD_INTERACTION,
                request_id=request_id,
                tenant_id=tenant_id,
                project_id=project_id,
                subject_key=subject_key,
                dedupe_key=str(payload.get("id") or "").strip() or None,
                event_type="interaction_create",
                payload_json=dict(payload),
                context_json={},
            ),
        )
        notify_webhook_job_enqueued(
            session,
            transport=WEBHOOK_TRANSPORT_DISCORD_INTERACTION,
            tenant_id=tenant_id,
            project_id=project_id,
            subject_key=subject_key,
            job_id=enqueue_result.job.job_id,
            dedupe_key=enqueue_result.job.dedupe_key,
        )
        session.commit()
        _close_deferred_interaction_work(result=result)
    http_envelope = TransportEnvelope(
        transport="discord_http",
        event_type="interaction_create",
        request_id=request_id,
        payload=payload,
    )
    return execute_http_ingress_result(
        result=_http_result_from_interaction_result(result),
        envelope=http_envelope,
    )


def _resolve_interaction_subject_scope(*, session: Session, payload: dict) -> tuple[str | None, str | None, str]:
    channel_id = str(payload.get("channel_id") or "").strip()
    user_id = str(((payload.get("member") or {}).get("user") or {}).get("id") or "").strip()
    tenant_id: str | None = None
    project_id: str | None = None
    if channel_id:
        tenant = build_default_discord_interaction_dispatch_deps(
            task_scheduler=lambda coro: coro,
            logger=logger,
        ).find_tenant_for_discord_channel(session=session, channel_id=channel_id)
        if tenant is not None:
            tenant_id = str(getattr(tenant, "tenant_id", "") or "").strip() or None
    root_message_id = None
    data = payload.get("data")
    if isinstance(data, dict) and data.get("type") == 3:
        root_message_id = str(data.get("target_id") or "").strip() or None
    if root_message_id is None:
        message = payload.get("message")
        if isinstance(message, dict):
            root_message_id = str(message.get("id") or "").strip() or None
    if tenant_id and channel_id:
        context = resolve_followup_context(
            session=session,
            tenant_id=tenant_id,
            channel_id=channel_id,
            root_message_id=root_message_id,
        )
        if context is not None:
            project_id = str(getattr(context, "project_id", "") or "").strip() or None
            context_id = str(getattr(context, "context_id", "") or "").strip()
            if context_id:
                return tenant_id, project_id, f"discord_followup:{context_id}"
    if tenant_id and channel_id:
        return tenant_id, project_id, f"discord_channel:{tenant_id}:{channel_id}"
    if tenant_id and user_id:
        return tenant_id, project_id, f"discord_user:{tenant_id}:{user_id}"
    if channel_id:
        return None, None, f"discord_channel::{channel_id}"
    if user_id:
        return None, None, f"discord_user::{user_id}"
    return None, None, "discord_interaction:unknown"


def _close_deferred_interaction_work(*, result: IngressResult) -> None:
    for deferred in result.deferred_work:
        awaitable = deferred.runner()
        close = getattr(awaitable, "close", None)
        if callable(close):
            close()
