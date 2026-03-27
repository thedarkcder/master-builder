from __future__ import annotations

import logging
from uuid import uuid4

from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from orchestrator.api.transport_runtime import http_json_response_action, json_response_to_action, execute_http_ingress_result
from orchestrator.api.webhooks.github_webhook_context import resolve_github_webhook_context
from orchestrator.core.communications import IngressResult
from orchestrator.core.communications import TransportEnvelope
from orchestrator.core.webhook_job_queue import (
    WEBHOOK_TRANSPORT_GITHUB,
    WebhookJobEnqueueRequest,
    enqueue_webhook_job,
)
from orchestrator.storage.run_queue_events import notify_webhook_job_enqueued

logger = logging.getLogger(__name__)


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
    resolved_context = await resolve_github_webhook_context(
        request=request,
        session=session,
        settings=settings,
        request_id=envelope.request_id,
        logger=logger,
    )
    if isinstance(resolved_context, JSONResponse):
        return execute_http_ingress_result(
            result=IngressResult(actions=(json_response_to_action(resolved_context),)),
            envelope=envelope,
        )
    queued_jobs = []
    for pr_number, review_summary_present in resolved_context.pr_targets:
        enqueue_result = enqueue_webhook_job(
            session,
            request=WebhookJobEnqueueRequest(
                transport=WEBHOOK_TRANSPORT_GITHUB,
                request_id=envelope.request_id,
                tenant_id=resolved_context.tenant.tenant_id,
                project_id=resolved_context.project.project_id,
                subject_key=(
                    f"github_pr:{resolved_context.tenant.tenant_id}:"
                    f"{resolved_context.repo_full_name}:{pr_number}"
                ),
                dedupe_key=(
                    f"{resolved_context.delivery_id}:{pr_number}"
                    if str(resolved_context.delivery_id or "").strip()
                    else None
                ),
                event_type=resolved_context.github_event,
                payload_json=dict(resolved_context.payload),
                context_json={
                    "delivery_id": resolved_context.delivery_id,
                    "github_event": resolved_context.github_event,
                    "normalized_action": resolved_context.normalized_action,
                    "installation_id": resolved_context.installation_id,
                    "tenant_id": resolved_context.tenant.tenant_id,
                    "project_id": resolved_context.project.project_id,
                    "repo_full_name": resolved_context.repo_full_name,
                    "pr_number": pr_number,
                    "review_summary_present": bool(review_summary_present),
                },
            ),
        )
        queued_jobs.append(enqueue_result)
        notify_webhook_job_enqueued(
            session,
            transport=WEBHOOK_TRANSPORT_GITHUB,
            tenant_id=resolved_context.tenant.tenant_id,
            project_id=resolved_context.project.project_id,
            subject_key=enqueue_result.job.subject_key,
            job_id=enqueue_result.job.job_id,
            dedupe_key=enqueue_result.job.dedupe_key,
        )
    session.commit()
    created_jobs = [job for job in queued_jobs if job.created]
    return execute_http_ingress_result(
        result=IngressResult(
            actions=(
                http_json_response_action(
                    status_code=202,
                    content={
                        "request_id": envelope.request_id,
                        "delivery_id": resolved_context.delivery_id,
                        "tenant_id": resolved_context.tenant.tenant_id,
                        "project_id": resolved_context.project.project_id,
                        "event": resolved_context.github_event,
                        "action": resolved_context.normalized_action,
                        "accepted": True,
                        "queued": bool(created_jobs),
                        "queued_job_count": len(created_jobs),
                        "repository": resolved_context.repo_full_name,
                        "pr_numbers": [context_job.job.context_json["pr_number"] for context_job in queued_jobs],
                    },
                ),
            ),
        ),
        envelope=envelope,
    )
