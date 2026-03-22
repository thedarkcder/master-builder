from __future__ import annotations

from contextlib import nullcontext

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from orchestrator.api.dependencies import get_session
from orchestrator.api.transport_runtime import execute_http_ingress_result
from orchestrator.api.webhooks.contracts import (
    parse_jira_comment_command as _parse_jira_comment_command,
    post_jira_comment as _post_jira_comment,
)
from orchestrator.api.webhooks.jira_application import build_jira_webhook_ingress_result
from orchestrator.core.webhook_health import webhook_health_tracker
from orchestrator.core.communications import TransportEnvelope
from orchestrator.core.config import get_settings
from orchestrator.core.discord.transport_executor import DiscordTransportExecutor

router = APIRouter(tags=["jira-webhook"])
__all__ = [
    "router",
    "ingest_jira_webhook",
    "_parse_jira_comment_command",
    "_post_jira_comment",
]
@router.post("/jira/webhook/{tenant_id}")
async def ingest_jira_webhook(
    tenant_id: str,
    request: Request,
    session: Session = Depends(get_session),
) -> dict:
    try:
        settings = get_settings()
        result = await build_jira_webhook_ingress_result(
            tenant_id=tenant_id,
            request=request,
            session=session,
            settings=settings,
            envelope=TransportEnvelope(
                transport="jira_webhook",
                event_type=str(request.headers.get("X-Atlassian-Webhook-Identifier") or "").strip() or "jira_webhook",
                request_id=request.headers.get("X-Request-Id") or request.headers.get("X-Atlassian-Webhook-Identifier") or tenant_id,
            ),
        )
        webhook_health_tracker.record(tenant_id=tenant_id, outcome="received")
        return execute_http_ingress_result(
            result=result,
            transport_action_executors=(
                DiscordTransportExecutor(
                    session_factory=lambda: nullcontext(session),
                    settings_factory=lambda: settings,
                ),
            ),
        )
    except HTTPException as exc:
        if exc.status_code >= 400 and exc.status_code != 404:
            webhook_health_tracker.record(tenant_id=tenant_id, outcome="failed")
        raise
    except Exception:
        webhook_health_tracker.record(tenant_id=tenant_id, outcome="failed")
        raise
