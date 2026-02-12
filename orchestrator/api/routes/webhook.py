from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from orchestrator.api.dependencies import get_session
from orchestrator.api.webhooks.contracts import (
    parse_jira_comment_command as _parse_jira_comment_command,
    post_jira_comment as _post_jira_comment,
)
from orchestrator.core.webhook_health import webhook_health_tracker
from orchestrator.api.webhooks.jira_ingress import ingest_jira_webhook_event
from orchestrator.core.config import get_settings

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
    webhook_health_tracker.record(tenant_id=tenant_id, outcome="received")
    try:
        return await ingest_jira_webhook_event(
            tenant_id=tenant_id,
            request=request,
            session=session,
            settings=get_settings(),
        )
    except HTTPException as exc:
        if exc.status_code >= 400:
            webhook_health_tracker.record(tenant_id=tenant_id, outcome="failed")
        raise
    except Exception:
        webhook_health_tracker.record(tenant_id=tenant_id, outcome="failed")
        raise
