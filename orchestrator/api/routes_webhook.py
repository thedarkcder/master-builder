from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.api.webhook_payload_utils import (
    read_json_payload as _read_json_payload,
)
from orchestrator.api.discord_ask_context import (
    remove_issue_key_from_tenant_ask_history,
)
from orchestrator.api.dependencies import get_session
from orchestrator.api.jira_oauth_connection_service import tenant_jira_oauth_context
from orchestrator.api.command_entrypoint import (
    execute_tenant_jira_comment_command,
)
from orchestrator.api.schemas import DiscordCommandRequest
from orchestrator.core.config import get_settings
from orchestrator.api.webhooks.contracts import (
    JIRA_COMMENT_EVENTS,
    extract_delivery_id as _extract_delivery_id,
    extract_issue_payload as _extract_issue_payload,
    extract_jira_comment_author_account_id as _extract_jira_comment_author_account_id,
    extract_status_transition as _extract_status_transition,
    normalize_jira_webhook_event as _normalize_jira_webhook_event,
    parse_jira_comment_command as _parse_jira_comment_command,
    post_jira_comment as _post_jira_comment,
    record_jira_webhook_receipt as _record_jira_webhook_receipt,
    resolve_active_project_for_issue as _resolve_active_project_for_issue,
    resolve_active_project_for_repo as _resolve_active_project_for_repo,
    validate_github_webhook_signature as _validate_github_webhook_signature,
    validate_webhook_auth as _validate_webhook_auth,
    resolve_global_github_webhook_secret as _resolve_global_github_webhook_secret,
    resolve_tenant_github_webhook_secret as _resolve_tenant_github_webhook_secret,
    extract_installation_id as _extract_installation_id,
    find_tenant_by_installation_id as _find_tenant_by_installation_id,
    extract_repository_full_name as _extract_repository_full_name,
    extract_pull_request_targets as _extract_pull_request_targets,
)
from orchestrator.core.runs import (
    RUN_STATUS_BLOCKED,
    RUN_STATUS_CANCELLED,
    RUN_STATUS_FAILED,
    enqueue_run,
)
from orchestrator.core.secret_manager import resolve_scoped_secret_ref
from orchestrator.core.signal_templates import format_discord_ready_gate_guidance
from orchestrator.storage.models import Project, Run, Tenant

router = APIRouter(tags=["jira-webhook"])

# Canonical command-ingress entrypoints by source.
execute_jira_comment_command = execute_tenant_jira_comment_command

logger = logging.getLogger(__name__)
ISSUE_KEY_PATTERN = re.compile(r"\b[A-Z][A-Z0-9_]+-\d+\b")


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




def _jira_webhook_response(
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


async def _stage_parse_jira_webhook_context(
    *,
    tenant_id: str,
    tenant: Tenant,
    request: Request,
    request_id: str,
    session: Session,
    settings,  # noqa: ANN001
) -> JiraWebhookContext:
    _validate_webhook_auth(
        tenant=tenant,
        request=request,
        request_id=request_id,
        session=session,
        settings=settings,
    )

    payload, _ = await _read_json_payload(request, request_id=request_id, source="jira")
    webhook_event = _normalize_jira_webhook_event(payload.get("webhookEvent"))

    issue_key, _labels, issue_status, issue_status_category_key, issue_summary, issue_description = (
        _extract_issue_payload(payload)
    )
    comment_command, comment_command_argument, comment_command_error = _parse_jira_comment_command(payload)
    delivery_id = _extract_delivery_id(request)
    _record_jira_webhook_receipt(
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
    project = _resolve_active_project_for_issue(
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


def _stage_handle_issue_deleted(
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
    return _jira_webhook_response(
        context,
        enqueued=False,
        reason="issue_deleted",
        removed_history_entries=removed_entries,
        webhook_event=context.webhook_event,
    )


def _stage_handle_invalid_comment_command(
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
    return _jira_webhook_response(
        context,
        enqueued=False,
        reason="invalid_comment_command",
        webhook_event=context.webhook_event,
    )


def _stage_handle_comment_event_memory(
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


def _stage_handle_comment_without_command(
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
    return _jira_webhook_response(
        context,
        enqueued=False,
        reason="comment_without_command",
        webhook_event=context.webhook_event,
        removed_history_entries=removed_history_entries,
    )


def _stage_handle_comment_ask_command(
    *,
    context: JiraWebhookContext,
    session: Session,
    settings,  # noqa: ANN001
) -> dict | None:
    if context.comment_command != "ask":
        return None

    question = (context.comment_command_argument or "").strip()
    if not question:
        return _jira_webhook_response(
            context,
            enqueued=False,
            reason="invalid_comment_command",
        )

    author_account_id = _extract_jira_comment_author_account_id(context.payload) or "jira-user"
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

    posted, post_error = _post_jira_comment(
        session=session,
        tenant=context.tenant,
        issue_key=context.issue_key,
        comment=response_text,
        settings=settings,
    )
    return _jira_webhook_response(
        context,
        enqueued=False,
        reason="comment_command_ask",
        command=context.comment_command,
        question=question,
        comment_posted=posted,
        comment_error=post_error,
        webhook_event=context.webhook_event,
    )


def _resolve_ready_statuses_for_tenant(tenant: Tenant) -> list[str]:
    configured_ready_statuses = tenant.jira_config.get("ready_statuses")
    if isinstance(configured_ready_statuses, list):
        ready_statuses = [str(status).strip() for status in configured_ready_statuses if str(status).strip()]
    else:
        ready_statuses = []
    return ready_statuses or ["Ready for Agent"]


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
