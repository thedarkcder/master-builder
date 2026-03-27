from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import status

from fastapi import HTTPException
from sqlalchemy.orm import Session

from orchestrator.api.webhooks.jira_comment_planner import plan_jira_comment_flow
from orchestrator.api.webhooks.jira_enqueue_planner import (
    build_jira_enqueue_skipped_notification_action,
    plan_jira_run_flow,
)
from orchestrator.api.webhooks.jira_event_classifier import evaluate_jira_trigger_state
from orchestrator.api.webhooks.jira_webhook_types import (
    JiraWebhookContext,
    JiraWebhookContextSnapshot,
    hydrate_jira_webhook_context,
    jira_webhook_response,
    snapshot_jira_webhook_context,
)
from orchestrator.core.webhook_job_queue import (
    WEBHOOK_TRANSPORT_JIRA,
    WebhookJobEnqueueRequest,
    enqueue_webhook_job,
)
from orchestrator.core.communications.enqueue_reason_contract import enqueue_reason_guidance
from orchestrator.core.communications import HttpJsonResponseAction, IngressResult, TransportAction, TransportEnvelope
from orchestrator.core.observability import reset_log_context, set_log_context
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import Tenant
from orchestrator.storage.run_queue_events import notify_webhook_job_enqueued

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class JiraWebhookPlan:
    content: dict
    actions: tuple[TransportAction, ...] = ()
    status_code: int = 200

async def build_jira_webhook_ingress_result(
    *,
    tenant_id: str,
    request,
    session: Session,
    settings,  # noqa: ANN001
    envelope: TransportEnvelope,
) -> IngressResult:
    context_tokens = set_log_context(correlation_id=envelope.request_id, tenant_id=tenant_id)
    try:
        from orchestrator.api.webhooks.jira_ingress import stage_parse_jira_webhook_context

        logger.info("jira_webhook_received request_id=%s tenant_id=%s", envelope.request_id, tenant_id)

        tenant = session.get(Tenant, tenant_id)
        if tenant is None:
            logger.warning("jira_webhook_unknown_tenant request_id=%s tenant_id=%s", envelope.request_id, tenant_id)
            raise HTTPException(status_code=404, detail="Unknown tenant")
        if not tenant.is_enabled:
            logger.info(
                "jira_webhook_ignored request_id=%s tenant_id=%s reason=tenant_disabled",
                envelope.request_id,
                tenant_id,
            )
            return _http_json_result(
                JiraWebhookPlan(
                    content={
                        "request_id": envelope.request_id,
                        "tenant_id": tenant_id,
                        "enqueued": False,
                        "reason": "tenant_disabled",
                    }
                )
            )

        context = await stage_parse_jira_webhook_context(
            tenant_id=tenant_id,
            tenant=tenant,
            request=request,
            request_id=envelope.request_id,
            session=session,
            settings=settings,
        )
        context_snapshot = snapshot_jira_webhook_context(context=context)
        enqueue_result = enqueue_webhook_job(
            session,
            request=WebhookJobEnqueueRequest(
                transport=WEBHOOK_TRANSPORT_JIRA,
                request_id=context.request_id,
                tenant_id=context.tenant_id,
                project_id=context.project.project_id if context.project is not None else None,
                subject_key=f"jira:{context.tenant_id}:{context.issue_key}",
                dedupe_key=context.delivery_id,
                event_type=context.webhook_event,
                payload_json=dict(context.payload),
                context_json={
                    "snapshot": {
                        "request_id": context_snapshot.request_id,
                        "tenant_id": context_snapshot.tenant_id,
                        "payload": dict(context_snapshot.payload),
                        "webhook_event": context_snapshot.webhook_event,
                        "issue_key": context_snapshot.issue_key,
                        "issue_labels": list(context_snapshot.issue_labels),
                        "issue_status": context_snapshot.issue_status,
                        "issue_status_category_key": context_snapshot.issue_status_category_key,
                        "issue_summary": context_snapshot.issue_summary,
                        "issue_description": context_snapshot.issue_description,
                        "comment_command": context_snapshot.comment_command,
                        "comment_command_argument": context_snapshot.comment_command_argument,
                        "comment_command_error": context_snapshot.comment_command_error,
                        "delivery_id": context_snapshot.delivery_id,
                        "project_id": context_snapshot.project_id,
                    }
                },
            ),
        )
        notify_webhook_job_enqueued(
            session,
            transport=WEBHOOK_TRANSPORT_JIRA,
            tenant_id=context.tenant_id,
            project_id=context.project.project_id if context.project is not None else None,
            subject_key=f"jira:{context.tenant_id}:{context.issue_key}",
            job_id=enqueue_result.job.job_id,
            dedupe_key=enqueue_result.job.dedupe_key,
        )
        session.commit()
        logger.info(
            "jira_webhook_job_enqueued request_id=%s tenant_id=%s issue_key=%s job_id=%s delivery_id=%s created=%s",
            context.request_id,
            context.tenant_id,
            context.issue_key,
            enqueue_result.job.job_id,
            context.delivery_id,
            enqueue_result.created,
        )
        return _http_json_result(
            JiraWebhookPlan(
                content={
                    "request_id": context.request_id,
                    "tenant_id": context.tenant_id,
                    "project_id": context.project.project_id if context.project is not None else None,
                    "issue_key": context.issue_key,
                    "delivery_id": context.delivery_id,
                    "accepted": True,
                    "enqueued": False,
                    "queued": enqueue_result.created,
                    "reason": "queued_for_reconciliation" if enqueue_result.created else "duplicate_delivery",
                    "job_id": enqueue_result.job.job_id,
                    "webhook_event": context.webhook_event,
                },
                status_code=status.HTTP_202_ACCEPTED,
            )
        )
    finally:
        reset_log_context(context_tokens)


def _process_jira_webhook_context(
    *,
    context: JiraWebhookContext,
    session: Session,
    settings,  # noqa: ANN001
) -> JiraWebhookPlan:
    comment_plan = plan_jira_comment_flow(
        context=context,
        session=session,
        settings=settings,
    )
    if comment_plan.content is not None:
        return JiraWebhookPlan(content=comment_plan.content)

    if context.project is None:
        logger.info(
            "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=project_not_mapped",
            context.request_id,
            context.tenant_id,
            context.issue_key,
        )
        return JiraWebhookPlan(
            content=jira_webhook_response(
                context,
                enqueued=False,
                reason="project_not_mapped",
                guidance=enqueue_reason_guidance("project_not_mapped"),
                command=context.comment_command,
                webhook_event=context.webhook_event,
            ),
            actions=(
                build_jira_enqueue_skipped_notification_action(
                    context=context,
                    reason="project_not_mapped",
                ),
            ),
        )

    run_plan = plan_jira_run_flow(
        context=context,
        session=session,
        settings=settings,
        evaluate_jira_trigger_state_fn=evaluate_jira_trigger_state,
        jira_webhook_response_fn=jira_webhook_response,
    )
    return JiraWebhookPlan(
        content=run_plan.content,
        actions=run_plan.actions,
    )


def _process_jira_webhook_context_in_thread(
    *,
    context_snapshot: JiraWebhookContextSnapshot,
    settings,  # noqa: ANN001
) -> JiraWebhookPlan:
    session_factory = create_session_factory(getattr(settings, "database_url", None))
    session = session_factory()
    try:
        context = hydrate_jira_webhook_context(
            snapshot=context_snapshot,
            session=session,
        )
        return _process_jira_webhook_context(
            context=context,
            session=session,
            settings=settings,
        )
    finally:
        session.close()


def _http_json_result(plan: JiraWebhookPlan) -> IngressResult:
    return IngressResult(
        actions=(
            *plan.actions,
            HttpJsonResponseAction(status_code=plan.status_code, content=plan.content),
        )
    )
