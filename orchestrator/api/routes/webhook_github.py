from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from orchestrator.api.dependencies import get_session
from orchestrator.api.transport_runtime import build_http_transport_action_executors, execute_http_ingress_result
from orchestrator.api.webhooks.github_application import build_github_webhook_ingress_result
from orchestrator.api.webhooks.github_webhook_context import prepare_github_webhook_runtime
from orchestrator.core.communications import TransportEnvelope
from orchestrator.core.config import get_settings

router = APIRouter(tags=["github-webhook"])
logger = logging.getLogger(__name__)


@router.post("/github/webhook")
async def ingest_github_webhook(
    request: Request,
    session: Session = Depends(get_session),
) -> object:
    request_id = request.headers.get("X-Request-Id") or request.headers.get("X-GitHub-Delivery") or "github-webhook"
    settings = get_settings()
    envelope = TransportEnvelope(
        transport="github_webhook",
        event_type=str(request.headers.get("X-GitHub-Event") or "").strip() or "unknown",
        request_id=request_id,
        delivery_id=str(request.headers.get("X-GitHub-Delivery") or "").strip() or None,
    )
    prepared_runtime = await prepare_github_webhook_runtime(
        request=request,
        session=session,
        settings=settings,
        request_id=envelope.request_id,
        logger=logger,
    )
    result = await build_github_webhook_ingress_result(
        prepared_runtime=prepared_runtime,
        request_id=envelope.request_id,
        session=session,
        settings=settings,
    )
    return execute_http_ingress_result(
        result=result,
        envelope=envelope,
        transport_action_executors=build_http_transport_action_executors(
            session=session,
            settings=settings,
            extra_transport_action_executors=getattr(prepared_runtime, "transport_action_executors", ()),
        ),
    )
