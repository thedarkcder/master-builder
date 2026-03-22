from __future__ import annotations

import json
import logging
from contextlib import nullcontext
from uuid import uuid4

from sqlalchemy.orm import Session

from orchestrator.api.transport_runtime import execute_http_ingress_result
from orchestrator.api.webhooks.payload_utils import read_json_payload as _read_json_payload
from orchestrator.api.webhooks.contracts import (
    extract_delivery_id,
    extract_issue_payload,
    extract_status_transition,
    normalize_jira_webhook_event,
    parse_jira_comment_command,
    record_jira_webhook_receipt,
    resolve_active_project_for_issue,
    validate_webhook_auth,
)
from orchestrator.api.webhooks.jira_webhook_types import (
    JiraWebhookContext,
    jira_webhook_response,
)
from orchestrator.api.webhooks.jira_application import build_jira_webhook_ingress_result
from orchestrator.core.communications import TransportEnvelope
from orchestrator.core.discord.transport_executor import DiscordTransportExecutor
from orchestrator.storage.models import Tenant

logger = logging.getLogger(__name__)

async def stage_parse_jira_webhook_context(
    *,
    tenant_id: str,
    tenant: Tenant,
    request,
    request_id: str,
    session: Session,
    settings,  # noqa: ANN001
) -> JiraWebhookContext:
    validate_webhook_auth(
        tenant=tenant,
        request=request,
        request_id=request_id,
        session=session,
        settings=settings,
    )

    payload, _ = await _read_json_payload(request, request_id=request_id, source="jira")
    webhook_event = normalize_jira_webhook_event(payload.get("webhookEvent"))

    issue_key, issue_labels, issue_status, issue_status_category_key, issue_summary, issue_description = extract_issue_payload(payload)
    comment_command, comment_command_argument, comment_command_error = parse_jira_comment_command(payload)
    delivery_id = extract_delivery_id(request)
    record_jira_webhook_receipt(
        session=session,
        tenant=tenant,
        delivery_id=delivery_id,
        issue_key=issue_key,
        webhook_event=webhook_event,
    )
    logger.info(
        "jira_webhook_issue_parsed request_id=%s tenant_id=%s issue_key=%s delivery_id=%s webhook_event=%s comment_command=%s comment_command_error=%s",
        request_id,
        tenant_id,
        issue_key,
        delivery_id,
        webhook_event,
        comment_command,
        comment_command_error,
    )
    project = resolve_active_project_for_issue(
        session=session,
        tenant_id=tenant_id,
        issue_key=issue_key,
    )
    return JiraWebhookContext(
        request_id=request_id,
        tenant_id=tenant_id,
        tenant=tenant,
        payload=payload,
        webhook_event=webhook_event,
        issue_key=issue_key,
        issue_labels=issue_labels,
        issue_status=issue_status,
        issue_status_category_key=issue_status_category_key,
        issue_summary=issue_summary,
        issue_description=issue_description,
        comment_command=comment_command,
        comment_command_argument=comment_command_argument,
        comment_command_error=comment_command_error,
        delivery_id=delivery_id,
        project=project,
    )


async def ingest_jira_webhook_event(
    *,
    tenant_id: str,
    request,
    session: Session,
    settings,  # noqa: ANN001
    request_id: str | None = None,
) -> dict:
    normalized_request_id = request_id or request.headers.get("X-Request-Id") or str(uuid4())
    result = await build_jira_webhook_ingress_result(
        tenant_id=tenant_id,
        request=request,
        session=session,
        settings=settings,
        envelope=TransportEnvelope(
            transport="jira_webhook",
            event_type=str(request.headers.get("X-Atlassian-Webhook-Identifier") or "").strip() or "jira_webhook",
            request_id=normalized_request_id,
        ),
    )
    response = execute_http_ingress_result(
        result=result,
        transport_action_executors=(
            DiscordTransportExecutor(
                session_factory=lambda: nullcontext(session),
                settings_factory=lambda: settings,
            ),
        ),
    )
    return json.loads(response.body.decode("utf-8"))


__all__ = [
    "JiraWebhookContext",
    "extract_status_transition",
    "ingest_jira_webhook_event",
    "jira_webhook_response",
    "stage_parse_jira_webhook_context",
]
