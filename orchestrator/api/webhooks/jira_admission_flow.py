from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from orchestrator.api.discord.shared.state import normalize_status_name
from orchestrator.api.jira_oauth.connection_service import tenant_jira_oauth_context
from orchestrator.api.webhooks.contracts import post_jira_comment
from orchestrator.api.webhooks.jira_webhook_types import JiraWebhookContext, TODO_STATUS
from orchestrator.core.communications import DiscordTenantNotificationAction, TransportAction
from orchestrator.core.communications.execution_admission_format import (
    present_jira_admission,
)
from orchestrator.core.communications.jira_enqueue_presentation import (
    format_jira_enqueue_skipped_message_from_admission,
    normalize_backlog_pre_run_check_text,
)
from orchestrator.core.decision_clarification_service import evaluate_issue_clarification_state
from orchestrator.core.decision_engine import DecisionEngineResult, DecisionEventInput
from orchestrator.core.decision_types import tenant_ready_label, tenant_ready_trigger_mode
from orchestrator.core.decision_state_machine import (
    ExecutionAdmissionReason,
    admission_from_enqueue_reason,
    build_execution_admission_block,
    resolve_execution_admission,
)
from orchestrator.core.pre_run_check import evaluate_pre_run_check
from orchestrator.core.precheck_question_lock import (
    build_precheck_questions_block,
    remove_precheck_questions_block,
    upsert_precheck_questions_block,
)
from orchestrator.core.run_gate_service import enqueue_issue_run_with_precheck

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class JiraRunPlan:
    content: dict
    actions: tuple[TransportAction, ...] = ()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _oauth_context_value(oauth_context: object, field: str) -> object | None:
    if isinstance(oauth_context, dict):
        return oauth_context.get(field)
    return getattr(oauth_context, field, None)


def is_todo_status(status_name: str) -> bool:
    return normalize_status_name(status_name) == TODO_STATUS


def resolve_ready_trigger_mode_for_tenant(tenant) -> str:  # noqa: ANN001
    return tenant_ready_trigger_mode(tenant)


def resolve_ready_label_for_tenant(tenant) -> str | None:  # noqa: ANN001
    return tenant_ready_label(tenant)


def build_jira_enqueue_skipped_notification_action(
    *,
    context,
    admission,
    extra_detail: str | None = None,
) -> DiscordTenantNotificationAction:
    message = format_jira_enqueue_skipped_message_from_admission(
        issue_key=context.issue_key,
        issue_status=context.issue_status,
        admission=admission,
        extra_detail=extra_detail,
    )
    return DiscordTenantNotificationAction(
        tenant_id=context.tenant_id,
        project_id=context.project.project_id,
        message=message,
    )


def plan_jira_enqueue(
    *,
    session,
    context,
    issue_description: str | None,
    precheck_outcome: str | None,
    required_worker_capability: str | None,
) -> object:  # noqa: ANN001
    return enqueue_issue_run_with_precheck(
        session,
        tenant_id=context.tenant_id,
        project_id=context.project.project_id,
        issue_key=context.issue_key,
        issue_summary=context.issue_summary,
        issue_description=issue_description,
        repo_url=context.project.github_repository,
        delivery_id=context.delivery_id,
        precheck_outcome=precheck_outcome,
        required_worker_capability=required_worker_capability,
        max_concurrent_runs=context.tenant.policy_config.get("max_concurrent_runs"),
    )


def evaluate_precheck_decision_with_labels(
    *,
    context: JiraWebhookContext,
    session: Session,
    settings,  # noqa: ANN001
    issue_description: str | None = None,
    idempotency_key: str | None = None,
) -> DecisionEngineResult:
    effective_description = context.issue_description if issue_description is None else issue_description

    def _publish_jira_comment(comment: str) -> tuple[bool, str | None]:
        return post_jira_comment(
            session=session,
            tenant=context.tenant,
            issue_key=context.issue_key,
            comment=comment,
            settings=settings,
        )

    result = evaluate_issue_clarification_state(
        session=session,
        tenant=context.tenant,
        project=context.project,
        event=DecisionEventInput(
            source="jira_webhook",
            event_type=str(context.webhook_event or "jira_webhook"),
            idempotency_key=(
                idempotency_key
                or f"{context.request_id}:{context.webhook_event or 'unknown'}:{context.issue_key}"
            ),
            issue_key=context.issue_key,
            issue_summary=context.issue_summary,
            issue_description=effective_description,
            issue_labels=context.issue_labels,
        ),
        settings=settings,
        tenant_jira_oauth_context_fn=tenant_jira_oauth_context,
        publish_jira_comment_fn=_publish_jira_comment,
        evaluate_pre_run_check_fn=evaluate_pre_run_check,
    )
    if result.issue_labels != context.issue_labels:
        context.issue_labels = list(result.issue_labels)
        logger.info(
            "jira_webhook_labels_applied request_id=%s tenant_id=%s issue_key=%s labels=%s",
            context.request_id,
            context.tenant_id,
            context.issue_key,
            ",".join(context.issue_labels),
        )
    _sync_precheck_questions_block(
        context=context,
        session=session,
        settings=settings,
        decision_result=result,
        current_description=effective_description,
    )
    return result


def build_backlog_pre_run_check(
    *,
    context: JiraWebhookContext,
    decision_result: DecisionEngineResult,
) -> dict[str, object]:
    pre_check = decision_result.decision.pre_check
    if pre_check is None:
        return {
            "outcome": "policy_eval_failed",
            "ready_label": resolve_ready_label_for_tenant(context.tenant),
            "ready_label_present": False,
            "required_worker_capability": "",
            "required_worker_label": "",
            "required_worker_label_present": True,
            "decision_gate_triggered": False,
            "decision_gate_reason": "Precheck policy evaluation failed",
            "gtd_valid": False,
            "gtd_missing_criteria": [],
            "gtd_clarification_questions": [],
            "auto_resolved_slots": list(decision_result.auto_resolved_slots),
            "cycle_id": decision_result.cycle_id,
        }
    decision_gate_reason = normalize_backlog_pre_run_check_text(pre_check.decision_gate_reason)
    return {
        "outcome": pre_check.outcome,
        "ready_label": pre_check.ready_label,
        "ready_label_present": pre_check.ready_label_present,
        "required_worker_capability": pre_check.required_worker_capability,
        "required_worker_label": pre_check.required_worker_label,
        "required_worker_label_present": pre_check.required_worker_label_present,
        "decision_gate_triggered": pre_check.decision_gate_triggered,
        "decision_gate_reason": decision_gate_reason,
        "gtd_valid": pre_check.gtd_valid,
        "gtd_missing_criteria": list(pre_check.gtd_missing_criteria),
        "gtd_clarification_questions": list(pre_check.gtd_clarification_questions),
        "auto_resolved_slots": list(decision_result.auto_resolved_slots),
        "cycle_id": decision_result.cycle_id,
    }


def _sync_precheck_questions_block(
    *,
    context: JiraWebhookContext,
    session: Session,
    settings,  # noqa: ANN001
    decision_result: DecisionEngineResult,
    current_description: str | None,
) -> None:
    if context.project is None or not str(context.issue_key or "").strip():
        return
    pre_check = decision_result.decision.pre_check
    if pre_check is None:
        return
    decision_gate_reason = (
        str(getattr(pre_check, "decision_gate_reason", "") or "").strip() or None
        if bool(getattr(pre_check, "decision_gate_triggered", False))
        else None
    )
    decision_gate_questions = [
        str(question).strip()
        for question in getattr(getattr(pre_check, "decision_gate", None), "questions", ())
        if str(question).strip()
    ]
    gtd_questions = [
        str(question).strip()
        for question in getattr(pre_check, "gtd_clarification_questions", ())
        if str(question).strip()
    ]
    block = build_precheck_questions_block(
        decision_gate_reason=decision_gate_reason,
        decision_gate_questions=decision_gate_questions,
        gtd_questions=gtd_questions,
    )
    next_description = (
        upsert_precheck_questions_block(current_description=str(current_description or ""), block=block)
        if block
        else remove_precheck_questions_block(current_description=str(current_description or ""))
    )
    if next_description.strip() == str(current_description or "").strip():
        return
    try:
        oauth = tenant_jira_oauth_context(session=session, tenant=context.tenant, settings=settings)
        oauth_client = _oauth_context_value(oauth, "client")
        oauth_connection = _oauth_context_value(oauth, "connection")
        oauth_access_token = _oauth_context_value(oauth, "access_token")
        cloud_id = getattr(oauth_connection, "cloud_id", None)
        if oauth_client is None or oauth_access_token is None or not str(cloud_id or "").strip():
            raise RuntimeError("Tenant Jira OAuth context is incomplete")
        oauth_client.update_issue_summary_and_description(
            access_token=str(oauth_access_token),
            cloud_id=str(cloud_id),
            issue_id_or_key=context.issue_key,
            summary=str(context.issue_summary or ""),
            description=next_description,
        )
        context.issue_description = next_description
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "jira_webhook_precheck_question_block_sync_failed request_id=%s tenant_id=%s issue_key=%s error=%s",
            context.request_id,
            context.tenant_id,
            context.issue_key,
            exc,
        )


def plan_jira_run_flow(
    *,
    context,
    session,
    settings,  # noqa: ANN001
    evaluate_jira_trigger_state_fn,
    jira_webhook_response_fn,
) -> JiraRunPlan:
    from orchestrator.api.webhooks import jira_webhook_board_gate

    backlog_followup_response = jira_webhook_board_gate.stage_handle_backlog_followup_issue_created(
        context=context
    )
    if backlog_followup_response is not None:
        return JiraRunPlan(content=backlog_followup_response)

    if context.issue_status is None:
        logger.info(
            "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=issue_status_missing",
            context.request_id,
            context.tenant_id,
            context.issue_key,
        )
        return JiraRunPlan(
            content=jira_webhook_response_fn(
                context,
                enqueued=False,
                reason="issue_status_missing",
                webhook_event=context.webhook_event,
            )
        )

    if context.issue_status_category_key == "done":
        logger.info(
            "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=issue_done issue_status=%s",
            context.request_id,
            context.tenant_id,
            context.issue_key,
            context.issue_status,
        )
        return JiraRunPlan(
            content=jira_webhook_response_fn(
                context,
                enqueued=False,
                reason="issue_done",
                issue_status=context.issue_status,
                webhook_event=context.webhook_event,
            )
        )

    board_gate_response = jira_webhook_board_gate.stage_handle_run_board_gate(
        context=context,
        session=session,
        settings=settings,
    )
    if board_gate_response is not None:
        return JiraRunPlan(
            content=board_gate_response.response,
            actions=board_gate_response.actions,
        )

    ready_trigger_mode = resolve_ready_trigger_mode_for_tenant(context.tenant)
    trigger_state = evaluate_jira_trigger_state_fn(
        context=context,
        session=session,
        ready_trigger_mode=ready_trigger_mode,
        now=_utcnow(),
    )
    trigger_reason = trigger_state.trigger_reason
    if trigger_state.trigger_mode_skip_reason is not None:
        logger.info(
            "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=ready_status_recheck_disabled trigger_mode=%s",
            context.request_id,
            context.tenant_id,
            context.issue_key,
            ready_trigger_mode,
        )
        return JiraRunPlan(
            content=jira_webhook_response_fn(
                context,
                enqueued=False,
                reason="ready_status_recheck_disabled",
                trigger_reason=trigger_reason,
                trigger_mode=ready_trigger_mode,
                issue_status=context.issue_status,
                webhook_event=context.webhook_event,
            )
        )
    logger.info(
        "jira_webhook_trigger request_id=%s tenant_id=%s issue_key=%s trigger_reason=%s issue_status=%s from_status=%s to_status=%s",
        context.request_id,
        context.tenant_id,
        context.issue_key,
        trigger_reason,
        context.issue_status,
        trigger_state.from_status,
        trigger_state.to_status,
    )

    if not is_todo_status(context.issue_status):
        admission = build_execution_admission_block(
            reason=ExecutionAdmissionReason.READY_FOR_AGENT_BACKLOG,
        )
        admission_presentation = present_jira_admission(admission=admission)
        logger.info(
            "jira_webhook_not_started request_id=%s tenant_id=%s issue_key=%s reason=ready_for_agent_backlog issue_status=%s",
            context.request_id,
            context.tenant_id,
            context.issue_key,
            context.issue_status,
        )
        return JiraRunPlan(
            content=jira_webhook_response_fn(
                context,
                enqueued=False,
                ready_for_agent=True,
                trigger_reason=trigger_reason,
                webhook_event=context.webhook_event,
                **admission_presentation.response_fields,
            )
        )

    if trigger_state.cooldown_block is not None:
        logger.info(
            "jira_webhook_not_started request_id=%s tenant_id=%s issue_key=%s reason=decision_gate_cooldown_active remaining_seconds=%s run_id=%s",
            context.request_id,
            context.tenant_id,
            context.issue_key,
            trigger_state.cooldown_block.remaining_seconds,
            trigger_state.cooldown_block.run_id,
        )
        return JiraRunPlan(
            content=jira_webhook_response_fn(
                context,
                enqueued=False,
                reason="decision_gate_cooldown_active",
                guidance=(
                    "Decision Gate was recently required for this issue. "
                    "Wait for the cooldown to expire, then rerun."
                ),
                run_id=trigger_state.cooldown_block.run_id,
                cooldown_seconds_remaining=trigger_state.cooldown_block.remaining_seconds,
                ready_for_agent=True,
                trigger_reason=trigger_reason,
                webhook_event=context.webhook_event,
            )
        )

    retry_resolution = trigger_state.retry_resolution
    if retry_resolution.missing_retryable_run:
        logger.info(
            "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=no_retryable_run",
            context.request_id,
            context.tenant_id,
            context.issue_key,
        )
        content_admission = build_execution_admission_block(
            reason=ExecutionAdmissionReason.NO_RETRYABLE_RUN,
        )
        content_presentation = present_jira_admission(admission=content_admission)
        return JiraRunPlan(
            content=jira_webhook_response_fn(
                context,
                enqueued=False,
                **content_presentation.response_fields,
                trigger_reason=trigger_reason,
                webhook_event=context.webhook_event,
            ),
            actions=(
                build_jira_enqueue_skipped_notification_action(
                    context=context,
                    admission=content_admission,
                ),
            ),
        )

    resolved_issue_description = retry_resolution.issue_description
    decision_result = evaluate_precheck_decision_with_labels(
        context=context,
        session=session,
        settings=settings,
        issue_description=resolved_issue_description,
    )
    precheck_decision = decision_result.decision
    admission = resolve_execution_admission(decision_result=decision_result)
    if admission.blocked and admission.reason is ExecutionAdmissionReason.POLICY_EVAL_FAILED:
        logger.warning(
            "jira_webhook_not_started request_id=%s tenant_id=%s issue_key=%s reason=policy_eval_failed error=%s",
            context.request_id,
            context.tenant_id,
            context.issue_key,
            precheck_decision.policy_error,
        )
        content_admission = build_execution_admission_block(
            reason=ExecutionAdmissionReason.POLICY_EVAL_FAILED,
            detail=precheck_decision.policy_error,
        )
        content_presentation = present_jira_admission(admission=content_admission)
        return JiraRunPlan(
            content=jira_webhook_response_fn(
                context,
                enqueued=False,
                trigger_reason=trigger_reason,
                webhook_event=context.webhook_event,
                **content_presentation.response_fields,
            ),
            actions=(
                build_jira_enqueue_skipped_notification_action(
                    context=context,
                    admission=content_admission,
                    extra_detail=precheck_decision.policy_error,
                ),
            ),
        )
    if admission.blocked:
        admission_presentation = present_jira_admission(admission=admission)
        logger.info(
            "jira_webhook_not_started request_id=%s tenant_id=%s issue_key=%s reason=%s",
            context.request_id,
            context.tenant_id,
            context.issue_key,
            admission.reason_code,
        )
        return JiraRunPlan(
            content=jira_webhook_response_fn(
                context,
                enqueued=False,
                trigger_reason=trigger_reason,
                webhook_event=context.webhook_event,
                **admission_presentation.response_fields,
            ),
            actions=(
                build_jira_enqueue_skipped_notification_action(
                    context=context,
                    admission=admission,
                    extra_detail=admission_presentation.notification_detail,
                ),
            ),
        )

    enqueue_result = plan_jira_enqueue(
        session=session,
        context=context,
        issue_description=resolved_issue_description,
        precheck_outcome=admission.precheck_outcome,
        required_worker_capability=admission.required_worker_capability,
    )
    if not enqueue_result.enqueued:
        logger.info(
            "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=%s run_id=%s",
            context.request_id,
            context.tenant_id,
            context.issue_key,
            enqueue_result.reason,
            enqueue_result.run.run_id,
        )
        enqueue_admission = admission_from_enqueue_reason(raw_reason=enqueue_result.reason)
        enqueue_presentation = present_jira_admission(admission=enqueue_admission)
        return JiraRunPlan(
            content=jira_webhook_response_fn(
                context,
                enqueued=False,
                run_id=enqueue_result.run.run_id,
                trigger_reason=trigger_reason,
                command=context.comment_command,
                webhook_event=context.webhook_event,
                **enqueue_presentation.response_fields,
            ),
            actions=(
                build_jira_enqueue_skipped_notification_action(
                    context=context,
                    admission=enqueue_admission,
                    extra_detail=f"run_id={enqueue_result.run.run_id}",
                ),
            ),
        )
    logger.info(
        "jira_webhook_enqueued request_id=%s tenant_id=%s issue_key=%s run_id=%s",
        context.request_id,
        context.tenant_id,
        context.issue_key,
        enqueue_result.run.run_id,
    )
    return JiraRunPlan(
        content=jira_webhook_response_fn(
            context,
            enqueued=True,
            reason=None,
            run_id=enqueue_result.run.run_id,
            trigger_reason=trigger_reason,
            command=context.comment_command,
            webhook_event=context.webhook_event,
        )
    )
