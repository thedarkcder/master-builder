from __future__ import annotations

from uuid import uuid4

from sqlalchemy.orm import Session

from orchestrator.api.transport_runtime import execute_http_ingress_result
from orchestrator.api.webhooks.github_application import build_github_webhook_ingress_result
from orchestrator.core.communications import TransportEnvelope


async def ingest_github_webhook_event(
    *,
    request,
    session: Session,
    settings,  # noqa: ANN001
    request_id: str | None = None,
):
    normalized_request_id = request_id or request.headers.get("X-Request-Id") or str(uuid4())
    result = await build_github_webhook_ingress_result(
        request=request,
        session=session,
        settings=settings,
        envelope=TransportEnvelope(
            transport="github_webhook",
            event_type=str(request.headers.get("X-GitHub-Event") or "").strip() or "unknown",
            request_id=normalized_request_id,
            delivery_id=str(request.headers.get("X-GitHub-Delivery") or "").strip() or None,
        ),
    )
    return execute_http_ingress_result(result=result)
