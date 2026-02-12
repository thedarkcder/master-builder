from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.api.commands.entrypoint import execute_tenant_jira_comment_command
from orchestrator.api.discord.ask.context import remove_issue_key_from_tenant_ask_history
from orchestrator.api.schemas import DiscordCommandRequest
from orchestrator.api.webhooks.payload_utils import read_json_payload as _read_json_payload
from orchestrator.api.webhooks.contracts import (
    JIRA_COMMENT_EVENTS,
    extract_delivery_id,
    extract_issue_payload,
    extract_jira_comment_author_account_id,
    extract_status_transition,
    normalize_jira_webhook_event,
    parse_jira_comment_command,
    post_jira_comment,
    record_jira_webhook_receipt,
    resolve_active_project_for_issue,
    validate_webhook_auth,
)
from orchestrator.core.runs import RUN_STATUS_BLOCKED, RUN_STATUS_CANCELLED, RUN_STATUS_FAILED, enqueue_run
from orchestrator.core.signal_templates import format_discord_ready_gate_guidance
from orchestrator.storage.models import Run
from orchestrator.storage.models import Project, Tenant

logger = logging.getLogger(__name__)

execute_jira_comment_command = execute_tenant_jira_comment_command
NON_ENQUEUE_ISSUE_WEBHOOK_EVENTS = {"issue_created"}


@dataclass
class JiraWebhookContext:
    request_id: str
    tenant_id: str
    tenant: Tenant
    payload: dict
    webhook_event: str | None
    issue_key: str
    issue_status: str | None
    issue_status_category_key: str | None
    issue_summary: str | None
    issue_description: str | None
    comment_command: str | None
    comment_command_argument: str | None
    comment_command_error: str | None
    delivery_id: str | None
    project: Project | None


def jira_webhook_response(
    context: JiraWebhookContext,
    *,
    enqueued: bool,
    reason: str | None,
    **extra: object,
) -> dict:
    payload: dict[str, object] = {
        "request_id": context.request_id,
        "tenant_id": context.tenant_id,
        "project_id": context.project.project_id if context.project is not None else None,
        "issue_key": context.issue_key,
        "enqueued": enqueued,
    }
    if reason is not None:
        payload["reason"] = reason
    payload.update(extra)
    return payload


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

    issue_key, _labels, issue_status, issue_status_category_key, issue_summary, issue_description = extract_issue_payload(payload)
    comment_command, comment_command_argument, comment_command_error = parse_jira_comment_command(payload)
    delivery_id = extract_delivery_id(request)
    record_jira_webhook_receipt(
        session=session,
        tenant=tenant,
        delivery_id=delivery_id,
        issue_key=issue_key,
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


def stage_handle_issue_deleted(
    *,
    context: JiraWebhookContext,
    session: Session,
) -> dict | None:
    if context.webhook_event != "issue_deleted":
        return None

    removed_entries = remove_issue_key_from_tenant_ask_history(
        session=session,
        tenant=context.tenant,
        issue_key=context.issue_key,
    )
    logger.info(
        "jira_webhook_issue_deleted request_id=%s tenant_id=%s issue_key=%s removed_history_entries=%s",
        context.request_id,
        context.tenant_id,
        context.issue_key,
        removed_entries,
    )
    return jira_webhook_response(
        context,
        enqueued=False,
        reason="issue_deleted",
        removed_history_entries=removed_entries,
        webhook_event=context.webhook_event,
    )


def stage_handle_invalid_comment_command(
    *,
    context: JiraWebhookContext,
) -> dict | None:
    if not context.comment_command_error:
        return None
    logger.info(
        "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=invalid_comment_command",
        context.request_id,
        context.tenant_id,
        context.issue_key,
    )
    return jira_webhook_response(
        context,
        enqueued=False,
        reason="invalid_comment_command",
        webhook_event=context.webhook_event,
    )


def stage_handle_comment_event_memory(
    *,
    context: JiraWebhookContext,
    session: Session,
) -> int:
    if context.webhook_event not in JIRA_COMMENT_EVENTS:
        return 0
    removed_entries = remove_issue_key_from_tenant_ask_history(
        session=session,
        tenant=context.tenant,
        issue_key=context.issue_key,
    )
    logger.info(
        "jira_webhook_comment_event_memory_cleared request_id=%s tenant_id=%s issue_key=%s webhook_event=%s removed_history_entries=%s",
        context.request_id,
        context.tenant_id,
        context.issue_key,
        context.webhook_event,
        removed_entries,
    )
    return removed_entries


def stage_handle_comment_without_command(
    *,
    context: JiraWebhookContext,
    removed_history_entries: int,
) -> dict | None:
    if context.webhook_event not in JIRA_COMMENT_EVENTS or context.comment_command is not None:
        return None
    logger.info(
        "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=comment_without_command webhook_event=%s",
        context.request_id,
        context.tenant_id,
        context.issue_key,
        context.webhook_event,
    )
    return jira_webhook_response(
        context,
        enqueued=False,
        reason="comment_without_command",
        webhook_event=context.webhook_event,
        removed_history_entries=removed_history_entries,
    )


def stage_handle_comment_ask_command(
    *,
    context: JiraWebhookContext,
    session: Session,
    settings,  # noqa: ANN001
) -> dict | None:
    if context.comment_command != "ask":
        return None

    question = (context.comment_command_argument or "").strip()
    if not question:
        return jira_webhook_response(
            context,
            enqueued=False,
            reason="invalid_comment_command",
        )

    author_account_id = extract_jira_comment_author_account_id(context.payload) or "jira-user"
    try:
        ask_response = execute_jira_comment_command(
            session=session,
            tenant_id=context.tenant_id,
            payload=DiscordCommandRequest(
                user_id=author_account_id,
                channel_id=None,
                command=f"!ask @{context.issue_key} {question}",
            ),
        )
        response_text = ask_response.message.strip()
        if not response_text:
            response_text = "I processed your question but returned no response text."
    except HTTPException as exc:
        response_text = f"Unable to process `/mb ask`: {exc.detail}"

    posted, post_error = post_jira_comment(
        session=session,
        tenant=context.tenant,
        issue_key=context.issue_key,
        comment=response_text,
        settings=settings,
    )
    return jira_webhook_response(
        context,
        enqueued=False,
        reason="comment_command_ask",
        command=context.comment_command,
        question=question,
        comment_posted=posted,
        comment_error=post_error,
        webhook_event=context.webhook_event,
    )


def resolve_ready_statuses_for_tenant(tenant: Tenant) -> list[str]:
    configured_ready_statuses = tenant.jira_config.get("ready_statuses")
    if isinstance(configured_ready_statuses, list):
        ready_statuses = [str(status).strip() for status in configured_ready_statuses if str(status).strip()]
    else:
        ready_statuses = []
    return ready_statuses or ["Ready for Agent"]


async def ingest_jira_webhook_event(
    *,
    tenant_id: str,
    request,
    session: Session,
    settings,  # noqa: ANN001
    request_id: str | None = None,
) -> dict:
    request_id = request_id or request.headers.get("X-Request-Id") or str(uuid4())
    logger.info("jira_webhook_received request_id=%s tenant_id=%s", request_id, tenant_id)

    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        logger.warning("jira_webhook_unknown_tenant request_id=%s tenant_id=%s", request_id, tenant_id)
        raise HTTPException(status_code=404, detail="Unknown tenant")
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

    context = await stage_parse_jira_webhook_context(
        tenant_id=tenant_id,
        tenant=tenant,
        request=request,
        request_id=request_id,
        session=session,
        settings=settings,
    )

    deleted_response = stage_handle_issue_deleted(context=context, session=session)
    if deleted_response is not None:
        return deleted_response

    invalid_comment_response = stage_handle_invalid_comment_command(context=context)
    if invalid_comment_response is not None:
        return invalid_comment_response

    removed_history_entries = stage_handle_comment_event_memory(context=context, session=session)
    comment_without_command_response = stage_handle_comment_without_command(
        context=context,
        removed_history_entries=removed_history_entries,
    )
    if comment_without_command_response is not None:
        return comment_without_command_response

    comment_ask_response = stage_handle_comment_ask_command(
        context=context,
        session=session,
        settings=settings,
    )
    if comment_ask_response is not None:
        return comment_ask_response

    if context.webhook_event in NON_ENQUEUE_ISSUE_WEBHOOK_EVENTS:
        logger.info(
            "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=non_enqueue_issue_event webhook_event=%s",
            request_id,
            tenant_id,
            context.issue_key,
            context.webhook_event,
        )
        return jira_webhook_response(
            context,
            enqueued=False,
            reason="non_enqueue_issue_event",
            webhook_event=context.webhook_event,
        )

    ready_statuses = resolve_ready_statuses_for_tenant(tenant)
    if context.issue_status is None:
        logger.info(
            "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=issue_status_missing",
            request_id,
            tenant_id,
            context.issue_key,
        )
        return jira_webhook_response(
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
        return jira_webhook_response(
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
        return jira_webhook_response(
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
        return jira_webhook_response(
            context,
            enqueued=False,
            reason="project_not_mapped",
            command=context.comment_command,
            webhook_event=context.webhook_event,
        )

    from_status, to_status = extract_status_transition(context.payload)
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
            return jira_webhook_response(
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
        return jira_webhook_response(
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

    return jira_webhook_response(
        context,
        enqueued=True,
        reason=None,
        run_id=enqueue_result.run.run_id,
        trigger_reason=trigger_reason,
        command=context.comment_command,
        webhook_event=context.webhook_event,
    )


__all__ = [
    "JiraWebhookContext",
    "execute_jira_comment_command",
    "extract_status_transition",
    "ingest_jira_webhook_event",
    "jira_webhook_response",
    "resolve_ready_statuses_for_tenant",
    "stage_handle_comment_ask_command",
    "stage_handle_comment_event_memory",
    "stage_handle_comment_without_command",
    "stage_handle_invalid_comment_command",
    "stage_handle_issue_deleted",
    "stage_parse_jira_webhook_context",
]
