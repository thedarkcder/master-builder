from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy.orm import Session

from orchestrator.api.webhooks.payload_utils import read_json_payload as _read_json_payload
from orchestrator.api.webhooks import jira_webhook_board_gate, jira_webhook_comment_flow, jira_webhook_precheck
from orchestrator.api.webhooks.jira_trigger_policy import (
    resolve_decision_gate_cooldown_block,
    resolve_jira_trigger_decision,
    resolve_retry_source,
)
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
from orchestrator.core.communications.enqueue_reason_contract import enqueue_reason_guidance
from orchestrator.core.observability import reset_log_context, set_log_context
from orchestrator.core.run_gate_service import enqueue_issue_run_with_precheck, resolve_run_gate_block
from orchestrator.api.webhooks.jira_webhook_types import (
    DECISION_GATE_COOLDOWN,
    JiraWebhookContext,
    jira_webhook_response,
)
from orchestrator.storage.models import Tenant

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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

        deleted_response = jira_webhook_comment_flow.stage_handle_issue_deleted(
            context=context,
            session=session,
        )
        if deleted_response is not None:
            return deleted_response

        invalid_comment_response = jira_webhook_comment_flow.stage_handle_invalid_comment_command(
            context=context
        )
        if invalid_comment_response is not None:
            return invalid_comment_response

        removed_history_entries = jira_webhook_comment_flow.stage_handle_comment_event_memory(
            context=context,
            session=session,
        )
        comment_without_command_response = jira_webhook_comment_flow.stage_handle_comment_without_command(
            context=context,
            removed_history_entries=removed_history_entries,
        )
        if comment_without_command_response is not None:
            return comment_without_command_response

        comment_ask_response = jira_webhook_comment_flow.stage_handle_comment_ask_command(
            context=context,
            session=session,
            settings=settings,
        )
        if comment_ask_response is not None:
            return comment_ask_response

        backlog_followup_response = jira_webhook_board_gate.stage_handle_backlog_followup_issue_created(
            context=context
        )
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
            jira_webhook_precheck.notify_jira_enqueue_skipped(
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

        board_gate_response = jira_webhook_board_gate.stage_handle_run_board_gate(
            context=context,
            session=session,
            settings=settings,
        )
        if board_gate_response is not None:
            return board_gate_response

        from_status, to_status = extract_status_transition(context.payload)
        ready_trigger_mode = jira_webhook_precheck.resolve_ready_trigger_mode_for_tenant(context.tenant)
        trigger_decision = resolve_jira_trigger_decision(
            comment_command=context.comment_command,
            webhook_event=context.webhook_event,
            from_status=from_status,
            to_status=to_status,
            ready_trigger_mode=ready_trigger_mode,
        )
        trigger_reason = trigger_decision.trigger_reason
        if trigger_decision.trigger_mode_skip_reason is not None:
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

        if not jira_webhook_precheck.is_todo_status(context.issue_status):
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

        cooldown_block = resolve_decision_gate_cooldown_block(
            session=session,
            tenant_id=tenant_id,
            issue_key=context.issue_key,
            cooldown_window=DECISION_GATE_COOLDOWN,
            now=_utcnow(),
        )
        if cooldown_block is not None:
            logger.info(
                "jira_webhook_not_started request_id=%s tenant_id=%s issue_key=%s reason=decision_gate_cooldown_active remaining_seconds=%s run_id=%s",
                request_id,
                tenant_id,
                context.issue_key,
                cooldown_block.remaining_seconds,
                cooldown_block.run_id,
            )
            return jira_webhook_response(
                context,
                enqueued=False,
                reason="decision_gate_cooldown_active",
                guidance=(
                    "Decision Gate was recently required for this issue. "
                    "Wait for the cooldown to expire, then rerun."
                ),
                run_id=cooldown_block.run_id,
                cooldown_seconds_remaining=cooldown_block.remaining_seconds,
                ready_for_agent=True,
                trigger_reason=trigger_reason,
                webhook_event=context.webhook_event,
            )

        retry_resolution = resolve_retry_source(
            session=session,
            tenant_id=tenant_id,
            issue_key=context.issue_key,
            comment_command=context.comment_command,
            fallback_issue_description=context.issue_description,
        )
        if retry_resolution.missing_retryable_run:
            logger.info(
                "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=no_retryable_run",
                request_id,
                tenant_id,
                context.issue_key,
            )
            jira_webhook_precheck.notify_jira_enqueue_skipped(
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
        resolved_issue_description = retry_resolution.issue_description

        decision_result = jira_webhook_precheck.evaluate_precheck_decision_with_labels(
            context=context,
            session=session,
            settings=settings,
            issue_description=resolved_issue_description,
        )
        precheck_decision = decision_result.decision
        gate_block = resolve_run_gate_block(decision_result=decision_result)
        if gate_block is not None and gate_block.reason == "policy_eval_failed":
            logger.warning(
                "jira_webhook_not_started request_id=%s tenant_id=%s issue_key=%s reason=policy_eval_failed error=%s",
                request_id,
                tenant_id,
                context.issue_key,
                precheck_decision.policy_error,
            )
            jira_webhook_precheck.notify_jira_enqueue_skipped(
                context=context,
                session=session,
                settings=settings,
                reason="policy_eval_failed",
                extra_detail=precheck_decision.policy_error,
            )
            return jira_webhook_response(
                context,
                enqueued=False,
                reason="policy_eval_failed",
                guidance=enqueue_reason_guidance("policy_eval_failed"),
                trigger_reason=trigger_reason,
                webhook_event=context.webhook_event,
            )
        if gate_block is not None:
            logger.info(
                "jira_webhook_not_started request_id=%s tenant_id=%s issue_key=%s reason=%s",
                request_id,
                tenant_id,
                context.issue_key,
                gate_block.reason,
            )
            jira_webhook_precheck.notify_jira_enqueue_skipped(
                context=context,
                session=session,
                settings=settings,
                reason=gate_block.reason,
                extra_detail=(
                    f"decision_gate_reason={gate_block.decision_gate_reason}"
                    if gate_block.reason == "decision_gate_required"
                    else (
                        "missing_gtd=" + ", ".join(gate_block.gtd_missing_criteria)
                        if gate_block.reason == "gtd_required"
                        else None
                    )
                ),
            )
            return jira_webhook_response(
                context,
                enqueued=False,
                reason=gate_block.reason,
                guidance=gate_block.guidance,
                trigger_reason=trigger_reason,
                webhook_event=context.webhook_event,
                decision_gate_reason=gate_block.decision_gate_reason if gate_block.reason == "decision_gate_required" else None,
                gtd_missing_criteria=list(gate_block.gtd_missing_criteria) if gate_block.reason == "gtd_required" else None,
                gtd_questions=list(gate_block.gtd_questions) if gate_block.reason == "gtd_required" else None,
                ready_label=(
                    jira_webhook_precheck.resolve_ready_label_for_tenant(context.tenant)
                    if gate_block.reason == "missing_ready_label"
                    else None
                ),
            )

        enqueue_result = enqueue_issue_run_with_precheck(
            session,
            tenant_id=tenant_id,
            project_id=context.project.project_id,
            issue_key=context.issue_key,
            issue_summary=context.issue_summary,
            issue_description=resolved_issue_description,
            repo_url=context.project.github_repository,
            delivery_id=context.delivery_id,
            precheck_outcome=precheck_decision.pre_check.outcome if precheck_decision.pre_check is not None else None,
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
            jira_webhook_precheck.notify_jira_enqueue_skipped(
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
    "extract_status_transition",
    "ingest_jira_webhook_event",
    "jira_webhook_response",
    "stage_parse_jira_webhook_context",
]
