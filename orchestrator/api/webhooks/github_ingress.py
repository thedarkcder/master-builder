from __future__ import annotations

from contextlib import nullcontext
from uuid import uuid4

from sqlalchemy.orm import Session

from orchestrator.api.transport_runtime import execute_http_ingress_result
from orchestrator.api.webhooks.github_application import build_github_webhook_ingress_result
from orchestrator.core.communications import TransportEnvelope
from orchestrator.core.discord.transport_executor import DiscordTransportExecutor


async def ingest_github_webhook_event(
    *,
    request,
    session: Session,
    settings,  # noqa: ANN001
    request_id: str | None = None,
):
    normalized_request_id = request_id or request.headers.get("X-Request-Id") or str(uuid4())
    transport_action_executors = []
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
        register_transport_executor=transport_action_executors.append,
    )
    transport_action_executors.append(
        DiscordTransportExecutor(
            session_factory=lambda: nullcontext(session),
            settings_factory=lambda: settings,
        )
    )
    return execute_http_ingress_result(
        result=result,
        transport_action_executors=tuple(transport_action_executors),
    )
