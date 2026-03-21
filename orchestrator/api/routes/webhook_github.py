from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from orchestrator.api.dependencies import get_session
from orchestrator.api.transport_runtime import execute_http_ingress_result
from orchestrator.api.webhooks.github_application import build_github_webhook_ingress_result
from orchestrator.core.communications import TransportEnvelope
from orchestrator.core.config import get_settings

router = APIRouter(tags=["github-webhook"])


@router.post("/github/webhook")
async def ingest_github_webhook(
    request: Request,
    session: Session = Depends(get_session),
) -> object:
    request_id = request.headers.get("X-Request-Id") or request.headers.get("X-GitHub-Delivery") or "github-webhook"
    result = await build_github_webhook_ingress_result(
        request=request,
        session=session,
        settings=get_settings(),
        envelope=TransportEnvelope(
            transport="github_webhook",
            event_type=str(request.headers.get("X-GitHub-Event") or "").strip() or "unknown",
            request_id=request_id,
            delivery_id=str(request.headers.get("X-GitHub-Delivery") or "").strip() or None,
        ),
    )
    return execute_http_ingress_result(result=result)
