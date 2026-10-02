from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from orchestrator.api.dependencies import get_session
from orchestrator.api.webhooks.contracts import (
    parse_jira_comment_command as _parse_jira_comment_command,
    post_jira_comment as _post_jira_comment,
)
from orchestrator.api.webhooks.jira_ingress import ingest_jira_webhook_event
from orchestrator.api.webhooks.payload_utils import read_json_payload
from orchestrator.core.deployment_runtime import authorize_coolify_deployment_webhook
from orchestrator.core.webhooks.job_queue import (
    WEBHOOK_TRANSPORT_COOLIFY_DEPLOYMENT,
    WebhookJobEnqueueRequest,
    enqueue_webhook_job,
)
from orchestrator.core.webhooks.health import webhook_health_tracker
from orchestrator.core.config import get_settings
from orchestrator.storage.run_queue_events import notify_webhook_job_enqueued

router = APIRouter(tags=["jira-webhook"])
__all__ = [
    "router",
    "ingest_coolify_deployment_webhook",
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
        response = await ingest_jira_webhook_event(
            tenant_id=tenant_id,
            request=request,
            session=session,
            settings=get_settings(),
        )
        webhook_health_tracker.record(tenant_id=tenant_id, outcome="received")
        return response
    except HTTPException as exc:
        if exc.status_code >= 400 and exc.status_code != 404:
            webhook_health_tracker.record(tenant_id=tenant_id, outcome="failed")
        raise
    except Exception:
        webhook_health_tracker.record(tenant_id=tenant_id, outcome="failed")
        raise


@router.post(
    "/deployments/coolify/webhook/{tenant_id}/{project_id}/{token}",
    status_code=status.HTTP_202_ACCEPTED,
)
async def ingest_coolify_deployment_webhook(
    tenant_id: str,
    project_id: str,
    token: str,
    request: Request,
    session: Session = Depends(get_session),
) -> dict:
    authorize_coolify_deployment_webhook(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        webhook_token=token,
    )
    request_id = request.headers.get("X-Request-Id") or str(uuid4())
    payload, _ = await read_json_payload(
        request, request_id=request_id, source="coolify"
    )
    subject_key = f"coolify_deployment:{tenant_id}:{project_id}"
    enqueue_result = enqueue_webhook_job(
        session,
        request=WebhookJobEnqueueRequest(
            transport=WEBHOOK_TRANSPORT_COOLIFY_DEPLOYMENT,
            request_id=request_id,
            tenant_id=tenant_id,
            project_id=project_id,
            subject_key=subject_key,
            dedupe_key=request_id,
            event_type=str(
                payload.get("event_type")
                or payload.get("event")
                or payload.get("status")
                or ""
            ).strip()
            or None,
            payload_json=dict(payload),
            context_json={"webhook_token": token},
        ),
    )
    notify_webhook_job_enqueued(
        session,
        transport=WEBHOOK_TRANSPORT_COOLIFY_DEPLOYMENT,
        tenant_id=tenant_id,
        project_id=project_id,
        subject_key=subject_key,
        job_id=enqueue_result.job.job_id,
        dedupe_key=enqueue_result.job.dedupe_key,
    )
    session.commit()
    return {
        "ok": True,
        "queued": enqueue_result.created,
        "request_id": request_id,
        "tenant_id": tenant_id,
        "project_id": project_id,
        "job_id": enqueue_result.job.job_id,
        "transport": WEBHOOK_TRANSPORT_COOLIFY_DEPLOYMENT,
    }
