from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy.orm import Session

from orchestrator.api.webhooks.jira_comment_planner import plan_jira_comment_flow
from orchestrator.api.webhooks.jira_enqueue_planner import (
    plan_jira_run_flow,
)
from orchestrator.api.webhooks.jira_event_classifier import evaluate_jira_trigger_state
from orchestrator.api.webhooks.jira_webhook_types import (
    JiraWebhookContext,
    jira_webhook_response,
)
from orchestrator.core.communications import HttpJsonResponseAction, IngressResult, TransportAction, TransportEnvelope
from orchestrator.core.observability import reset_log_context, set_log_context
from orchestrator.storage.models import Tenant

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class JiraWebhookPlan:
    content: dict
    actions: tuple[TransportAction, ...] = ()

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
                {
                    "request_id": envelope.request_id,
                    "tenant_id": tenant_id,
                    "enqueued": False,
                    "reason": "tenant_disabled",
                }
            )

        context = await stage_parse_jira_webhook_context(
            tenant_id=tenant_id,
            tenant=tenant,
            request=request,
            request_id=envelope.request_id,
            session=session,
            settings=settings,
        )
        return _http_json_result(
            _process_jira_webhook_context(
                context=context,
                session=session,
                settings=settings,
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


def _http_json_result(plan: JiraWebhookPlan) -> IngressResult:
    return IngressResult(
        actions=(
            *plan.actions,
            HttpJsonResponseAction(status_code=200, content=plan.content),
        )
    )
