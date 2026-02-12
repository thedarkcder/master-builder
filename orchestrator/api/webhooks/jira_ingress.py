from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.api.commands.entrypoint import execute_tenant_jira_comment_command
from orchestrator.api.discord.ask.context import remove_issue_key_from_tenant_ask_history
from orchestrator.core.observability import reset_log_context, set_log_context
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
from orchestrator.core.communications.enqueue_reason_contract import enqueue_reason_guidance
from orchestrator.core.discord.notifications import send_tenant_discord_message
from orchestrator.core.runs import RUN_STATUS_BLOCKED, RUN_STATUS_CANCELLED, RUN_STATUS_FAILED, enqueue_run
from orchestrator.api.discord.shared.state import normalize_status_name
from orchestrator.storage.models import Run
from orchestrator.storage.models import Project, Tenant

logger = logging.getLogger(__name__)

execute_jira_comment_command = execute_tenant_jira_comment_command
TODO_STATUS = "to do"
DECISION_GATE_COOLDOWN = timedelta(minutes=10)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _is_todo_status(status_name: str) -> bool:
    return normalize_status_name(status_name) == TODO_STATUS


def _resolve_ready_trigger_mode_for_tenant(tenant: Tenant) -> str:
    raw_mode = tenant.jira_config.get("ready_trigger_mode")
    if isinstance(raw_mode, str):
        normalized_mode = raw_mode.strip().lower()
        if normalized_mode in {"status_recheck", "transition_only"}:
            return normalized_mode
    return "status_recheck"


def _latest_decision_gate_blocked_run(
    *,
    session: Session,
    tenant_id: str,
    issue_key: str,
) -> Run | None:
    return session.execute(
        select(Run)
        .where(
            Run.tenant_id == tenant_id,
            Run.issue_key == issue_key,
            Run.status == RUN_STATUS_BLOCKED,
            Run.last_error.is_not(None),
            Run.last_error.like("Decision Gate required:%"),
        )
        .order_by(Run.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def _decision_gate_cooldown_remaining_seconds(*, blocked_run: Run, now: datetime) -> int:
    blocked_at = blocked_run.finished_at or blocked_run.created_at
    if blocked_at is None:
        return 0
    if blocked_at.tzinfo is None:
        blocked_at = blocked_at.replace(tzinfo=timezone.utc)
    elapsed = now - blocked_at
    remaining = DECISION_GATE_COOLDOWN - elapsed
    return max(0, int(remaining.total_seconds()))


def _notify_jira_enqueue_skipped(
    *,
    context: JiraWebhookContext,
    session: Session,
    settings,  # noqa: ANN001
    reason: str,
    extra_detail: str | None = None,
) -> None:
    detail = f" ({extra_detail})" if extra_detail else ""
    guidance = enqueue_reason_guidance(reason)
    message = (
        f"Jira webhook did not queue a run for `{context.issue_key}`.\n"
        f"Reason: `{reason}`{detail}\n"
        f"Guidance: {guidance}\n"
        f"Status: `{context.issue_status or 'unknown'}`"
    )
    send_tenant_discord_message(
        session=session,
        tenant=context.tenant,
        project=context.project,
        message=message,
        settings=settings,
    )


@dataclass
class JiraWebhookContext:
    request_id: str
    tenant_id: str
    tenant: Tenant
    payload: dict
    webhook_event: str | None
    issue_key: str
    issue_labels: list[str]
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

    issue_key, issue_labels, issue_status, issue_status_category_key, issue_summary, issue_description = extract_issue_payload(payload)
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
        logger.exception(
            "jira_comment_ask_command_failed request_id=%s tenant_id=%s issue_key=%s detail=%s error=%s",
            context.request_id,
            context.tenant_id,
            context.issue_key,
            exc.detail,
            exc,
        )
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


def stage_handle_backlog_followup_issue_created(
    *,
    context: JiraWebhookContext,
) -> dict | None:
    if context.webhook_event != "issue_created":
        return None
    normalized_labels = {str(label).strip().casefold() for label in context.issue_labels}
    if "backlog-only" not in normalized_labels:
        return None
    logger.info(
        "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=backlog_followup_issue_created",
        context.request_id,
        context.tenant_id,
        context.issue_key,
    )
    return jira_webhook_response(
        context,
        enqueued=False,
        reason="backlog_followup_issue_created",
        webhook_event=context.webhook_event,
    )


async def ingest_jira_webhook_event(
    *,
    tenant_id: str,
    request,
    session: Session,
    settings,  # noqa: ANN001
    request_id: str | None = None,
) -> dict:
    request_id = request_id or request.headers.get("X-Request-Id") or str(uuid4())
    context_tokens = set_log_context(correlation_id=request_id, tenant_id=tenant_id)
    try:
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

        backlog_followup_response = stage_handle_backlog_followup_issue_created(context=context)
        if backlog_followup_response is not None:
            return backlog_followup_response

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

        if context.project is None:
            logger.info(
                "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=project_not_mapped",
                request_id,
                tenant_id,
                context.issue_key,
            )
            _notify_jira_enqueue_skipped(
                context=context,
                session=session,
                settings=settings,
                reason="project_not_mapped",
            )
            return jira_webhook_response(
                context,
                enqueued=False,
                reason="project_not_mapped",
                guidance=enqueue_reason_guidance("project_not_mapped"),
                command=context.comment_command,
                webhook_event=context.webhook_event,
            )

        from_status, to_status = extract_status_transition(context.payload)
        ready_trigger_mode = _resolve_ready_trigger_mode_for_tenant(context.tenant)
        trigger_reason = "status_recheck"
        if context.comment_command == "run":
            trigger_reason = "comment_command_run"
        elif context.comment_command == "retry":
            trigger_reason = "comment_command_retry"
        elif context.webhook_event == "issue_created":
            trigger_reason = "issue_created"
        elif (
            to_status is not None
            and from_status is not None
            and from_status.casefold() != to_status.casefold()
        ):
            trigger_reason = "status_transition_to_ready"
        if trigger_reason == "status_recheck" and ready_trigger_mode == "transition_only":
            logger.info(
                "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=ready_status_recheck_disabled trigger_mode=%s",
                request_id,
                tenant_id,
                context.issue_key,
                ready_trigger_mode,
            )
            return jira_webhook_response(
                context,
                enqueued=False,
                reason="ready_status_recheck_disabled",
                trigger_reason=trigger_reason,
                trigger_mode=ready_trigger_mode,
                issue_status=context.issue_status,
                webhook_event=context.webhook_event,
            )
        logger.info(
            "jira_webhook_trigger request_id=%s tenant_id=%s issue_key=%s trigger_reason=%s issue_status=%s from_status=%s to_status=%s",
            request_id,
            tenant_id,
            context.issue_key,
            trigger_reason,
            context.issue_status,
            from_status,
            to_status,
        )

        if not _is_todo_status(context.issue_status):
            logger.info(
                "jira_webhook_not_started request_id=%s tenant_id=%s issue_key=%s reason=ready_for_agent_backlog issue_status=%s",
                request_id,
                tenant_id,
                context.issue_key,
                context.issue_status,
            )
            return jira_webhook_response(
                context,
                enqueued=False,
                reason="ready_for_agent_backlog",
                ready_for_agent=True,
                trigger_reason=trigger_reason,
                webhook_event=context.webhook_event,
            )

        latest_decision_gate_block = _latest_decision_gate_blocked_run(
            session=session,
            tenant_id=tenant_id,
            issue_key=context.issue_key,
        )
        if latest_decision_gate_block is not None:
            remaining_seconds = _decision_gate_cooldown_remaining_seconds(
                blocked_run=latest_decision_gate_block,
                now=_utcnow(),
            )
            if remaining_seconds > 0:
                logger.info(
                    "jira_webhook_not_started request_id=%s tenant_id=%s issue_key=%s reason=decision_gate_cooldown_active remaining_seconds=%s run_id=%s",
                    request_id,
                    tenant_id,
                    context.issue_key,
                    remaining_seconds,
                    latest_decision_gate_block.run_id,
                )
                return jira_webhook_response(
                    context,
                    enqueued=False,
                    reason="decision_gate_cooldown_active",
                    guidance=(
                        "Decision Gate was recently required for this issue. "
                        "Wait for the cooldown to expire, then rerun."
                    ),
                    run_id=latest_decision_gate_block.run_id,
                    cooldown_seconds_remaining=remaining_seconds,
                    ready_for_agent=True,
                    trigger_reason=trigger_reason,
                    webhook_event=context.webhook_event,
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
                _notify_jira_enqueue_skipped(
                    context=context,
                    session=session,
                    settings=settings,
                    reason="no_retryable_run",
                )
                return jira_webhook_response(
                    context,
                    enqueued=False,
                    reason="no_retryable_run",
                    guidance=enqueue_reason_guidance("no_retryable_run"),
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
            _notify_jira_enqueue_skipped(
                context=context,
                session=session,
                settings=settings,
                reason=enqueue_result.reason,
                extra_detail=f"run_id={enqueue_result.run.run_id}",
            )
            return jira_webhook_response(
                context,
                enqueued=False,
                reason=enqueue_result.reason,
                guidance=enqueue_reason_guidance(enqueue_result.reason),
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
    finally:
        reset_log_context(context_tokens)


__all__ = [
    "JiraWebhookContext",
    "execute_jira_comment_command",
    "extract_status_transition",
    "ingest_jira_webhook_event",
    "jira_webhook_response",
    "stage_handle_comment_ask_command",
    "stage_handle_comment_event_memory",
    "stage_handle_comment_without_command",
    "stage_handle_invalid_comment_command",
    "stage_handle_issue_deleted",
    "stage_parse_jira_webhook_context",
]
