from __future__ import annotations

import logging
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
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
from orchestrator.core.followup_context_service import (
    resolve_discord_interaction_subject_scope as _resolve_interaction_subject_scope,
)
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
            find_tenant_for_discord_channel=build_default_discord_interaction_dispatch_deps(
                task_scheduler=lambda coro: coro,
                logger=logger,
            ).find_tenant_for_discord_channel,
        )
        if not tenant_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unable to resolve interaction tenant")
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


def _close_deferred_interaction_work(*, result: IngressResult) -> None:
    for deferred in result.deferred_work:
        awaitable = deferred.runner()
        close = getattr(awaitable, "close", None)
        if callable(close):
            close()
