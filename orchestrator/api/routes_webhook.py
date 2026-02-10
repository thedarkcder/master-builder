from __future__ import annotations

import logging
import re
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.api.dependencies import get_session
from orchestrator.api.webhooks.contracts import (
    parse_jira_comment_command as _parse_jira_comment_command,  # noqa: F401
    post_jira_comment as _post_jira_comment,  # noqa: F401
    extract_status_transition as _extract_status_transition,
)
from orchestrator.api.webhooks.jira_ingress import (
    jira_webhook_response as _jira_webhook_response,
    resolve_ready_statuses_for_tenant as _resolve_ready_statuses_for_tenant,
    stage_handle_comment_ask_command as _stage_handle_comment_ask_command,
    stage_handle_comment_event_memory as _stage_handle_comment_event_memory,
    stage_handle_comment_without_command as _stage_handle_comment_without_command,
    stage_handle_invalid_comment_command as _stage_handle_invalid_comment_command,
    stage_handle_issue_deleted as _stage_handle_issue_deleted,
    stage_parse_jira_webhook_context as _stage_parse_jira_webhook_context,
)
from orchestrator.core.config import get_settings
from orchestrator.core.runs import (
    RUN_STATUS_BLOCKED,
    RUN_STATUS_CANCELLED,
    RUN_STATUS_FAILED,
    enqueue_run,
)
from orchestrator.core.signal_templates import format_discord_ready_gate_guidance
from orchestrator.storage.models import Run, Tenant

router = APIRouter(tags=["jira-webhook"])

logger = logging.getLogger(__name__)
ISSUE_KEY_PATTERN = re.compile(r"\b[A-Z][A-Z0-9_]+-\d+\b")




@router.post("/jira/webhook/{tenant_id}")
async def ingest_jira_webhook(
    tenant_id: str,
    request: Request,
    session: Session = Depends(get_session),
) -> dict:
    settings = get_settings()
    request_id = request.headers.get("X-Request-Id") or str(uuid4())
    logger.info("jira_webhook_received request_id=%s tenant_id=%s", request_id, tenant_id)

    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        logger.warning("jira_webhook_unknown_tenant request_id=%s tenant_id=%s", request_id, tenant_id)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown tenant")
    if not tenant.is_enabled:
        logger.info(
            "jira_webhook_ignored request_id=%s tenant_id=%s reason=tenant_disabled",
            request_id,
            tenant_id,
        )
        return {
            "request_id": request_id,
            "tenant_id": tenant_id,
            "enqueued": False,
            "reason": "tenant_disabled",
        }

    context = await _stage_parse_jira_webhook_context(
        tenant_id=tenant_id,
        tenant=tenant,
        request=request,
        request_id=request_id,
        session=session,
        settings=settings,
    )

    deleted_response = _stage_handle_issue_deleted(context=context, session=session)
    if deleted_response is not None:
        return deleted_response

    invalid_comment_response = _stage_handle_invalid_comment_command(context=context)
    if invalid_comment_response is not None:
        return invalid_comment_response

    removed_history_entries = _stage_handle_comment_event_memory(context=context, session=session)
    comment_without_command_response = _stage_handle_comment_without_command(
        context=context,
        removed_history_entries=removed_history_entries,
    )
    if comment_without_command_response is not None:
        return comment_without_command_response

    comment_ask_response = _stage_handle_comment_ask_command(
        context=context,
        session=session,
        settings=settings,
    )
    if comment_ask_response is not None:
        return comment_ask_response

    ready_statuses = _resolve_ready_statuses_for_tenant(tenant)
    if context.issue_status is None:
        logger.info(
            "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=issue_status_missing",
            request_id,
            tenant_id,
            context.issue_key,
        )
        return _jira_webhook_response(
            context,
            enqueued=False,
            reason="issue_status_missing",
            webhook_event=context.webhook_event,
        )

    if context.issue_status_category_key == "done":
        logger.info(
            "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=issue_done issue_status=%s",
            request_id,
            tenant_id,
            context.issue_key,
            context.issue_status,
        )
        return _jira_webhook_response(
            context,
            enqueued=False,
            reason="issue_done",
            issue_status=context.issue_status,
            webhook_event=context.webhook_event,
        )

    normalized_ready_statuses = {status.casefold() for status in ready_statuses}
    if context.issue_status.casefold() not in normalized_ready_statuses:
        logger.info(
            "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=status_not_ready issue_status=%s",
            request_id,
            tenant_id,
            context.issue_key,
            context.issue_status,
        )
        return _jira_webhook_response(
            context,
            enqueued=False,
            reason="status_not_ready",
            issue_status=context.issue_status,
            ready_statuses=ready_statuses,
            guidance=format_discord_ready_gate_guidance(
                issue_key=context.issue_key,
                issue_status=context.issue_status,
                ready_statuses=ready_statuses,
            ),
            webhook_event=context.webhook_event,
        )

    if context.project is None:
        logger.info(
            "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=project_not_mapped",
            request_id,
            tenant_id,
            context.issue_key,
        )
        return _jira_webhook_response(
            context,
            enqueued=False,
            reason="project_not_mapped",
            command=context.comment_command,
            webhook_event=context.webhook_event,
        )

    from_status, to_status = _extract_status_transition(context.payload)
    trigger_reason = "ready_status_recheck"
    if context.comment_command == "run":
        trigger_reason = "comment_command_run"
    elif context.comment_command == "retry":
        trigger_reason = "comment_command_retry"
    elif (
        to_status is not None
        and to_status.casefold() in normalized_ready_statuses
        and from_status is not None
        and from_status.casefold() != to_status.casefold()
    ):
        trigger_reason = "status_transition_to_ready"
    logger.info(
        "jira_webhook_ready_trigger request_id=%s tenant_id=%s issue_key=%s trigger_reason=%s issue_status=%s from_status=%s to_status=%s",
        request_id,
        tenant_id,
        context.issue_key,
        trigger_reason,
        context.issue_status,
        from_status,
        to_status,
    )

    retry_source_run = None
    resolved_issue_description = context.issue_description
    if context.comment_command == "retry":
        retryable_statuses = {RUN_STATUS_FAILED, RUN_STATUS_BLOCKED, RUN_STATUS_CANCELLED}
        retry_source_run = session.execute(
            select(Run)
            .where(
                Run.tenant_id == tenant_id,
                Run.issue_key == context.issue_key,
                Run.status.in_(retryable_statuses),
            )
            .order_by(Run.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if retry_source_run is None:
            logger.info(
                "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=no_retryable_run",
                request_id,
                tenant_id,
                context.issue_key,
            )
            return _jira_webhook_response(
                context,
                enqueued=False,
                reason="no_retryable_run",
                trigger_reason=trigger_reason,
                webhook_event=context.webhook_event,
            )
        resolved_issue_description = retry_source_run.issue_description

    enqueue_result = enqueue_run(
        session,
        tenant_id=tenant_id,
        project_id=context.project.project_id,
        issue_key=context.issue_key,
        issue_summary=context.issue_summary,
        issue_description=resolved_issue_description,
        repo_url=context.project.github_repository,
        delivery_id=context.delivery_id,
        max_concurrent_runs=tenant.policy_config.get("max_concurrent_runs"),
    )
    if not enqueue_result.enqueued:
        logger.info(
            "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=%s run_id=%s",
            request_id,
            tenant_id,
            context.issue_key,
            enqueue_result.reason,
            enqueue_result.run.run_id,
        )
        return _jira_webhook_response(
            context,
            enqueued=False,
            reason=enqueue_result.reason,
            run_id=enqueue_result.run.run_id,
            trigger_reason=trigger_reason,
            command=context.comment_command,
            webhook_event=context.webhook_event,
        )
    logger.info(
        "jira_webhook_enqueued request_id=%s tenant_id=%s issue_key=%s run_id=%s",
        request_id,
        tenant_id,
        context.issue_key,
        enqueue_result.run.run_id,
    )

    return _jira_webhook_response(
        context,
        enqueued=True,
        reason=None,
        run_id=enqueue_result.run.run_id,
        trigger_reason=trigger_reason,
        command=context.comment_command,
        webhook_event=context.webhook_event,
    )
