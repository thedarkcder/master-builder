from __future__ import annotations

from uuid import uuid4

from sqlalchemy.orm import Session

from orchestrator.api.transport_runtime import build_http_transport_action_executors, execute_http_ingress_result
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
    envelope = TransportEnvelope(
        transport="github_webhook",
        event_type=str(request.headers.get("X-GitHub-Event") or "").strip() or "unknown",
        request_id=normalized_request_id,
        delivery_id=str(request.headers.get("X-GitHub-Delivery") or "").strip() or None,
    )
    transport_action_executors = []
    result = await build_github_webhook_ingress_result(
        request=request,
        session=session,
        settings=settings,
        envelope=envelope,
        register_transport_executor=transport_action_executors.append,
    )
    return execute_http_ingress_result(
        result=result,
        envelope=envelope,
        transport_action_executors=build_http_transport_action_executors(
            session=session,
            settings=settings,
            extra_transport_action_executors=transport_action_executors,
        ),
    )
