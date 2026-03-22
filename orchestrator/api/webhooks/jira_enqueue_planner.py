from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from orchestrator.api.webhooks import jira_webhook_board_gate, jira_webhook_precheck
from orchestrator.core.communications import DiscordTenantNotificationAction, TransportAction
from orchestrator.core.communications.enqueue_reason_contract import enqueue_reason_guidance
from orchestrator.core.run_gate_service import resolve_run_gate_block
from orchestrator.core.run_gate_service import enqueue_issue_run_with_precheck

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class JiraRunPlan:
    content: dict
    actions: tuple[TransportAction, ...] = ()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def plan_jira_enqueue(
    *,
    session,
    context,
    issue_description: str | None,
    precheck_decision,
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
        precheck_outcome=precheck_decision.pre_check.outcome if precheck_decision.pre_check is not None else None,
        max_concurrent_runs=context.tenant.policy_config.get("max_concurrent_runs"),
    )


def plan_jira_run_flow(
    *,
    context,
    session,
    settings,  # noqa: ANN001
    evaluate_jira_trigger_state_fn,
    jira_webhook_response_fn,
) -> JiraRunPlan:
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

    ready_trigger_mode = jira_webhook_precheck.resolve_ready_trigger_mode_for_tenant(context.tenant)
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

    if not jira_webhook_precheck.is_todo_status(context.issue_status):
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
                reason="ready_for_agent_backlog",
                ready_for_agent=True,
                trigger_reason=trigger_reason,
                webhook_event=context.webhook_event,
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
        return JiraRunPlan(
            content=jira_webhook_response_fn(
                context,
                enqueued=False,
                reason="no_retryable_run",
                guidance=enqueue_reason_guidance("no_retryable_run"),
                trigger_reason=trigger_reason,
                webhook_event=context.webhook_event,
            ),
            actions=(
                build_jira_enqueue_skipped_notification_action(
                    context=context,
                    reason="no_retryable_run",
                ),
            ),
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
            context.request_id,
            context.tenant_id,
            context.issue_key,
            precheck_decision.policy_error,
        )
        return JiraRunPlan(
            content=jira_webhook_response_fn(
                context,
                enqueued=False,
                reason="policy_eval_failed",
                guidance=enqueue_reason_guidance("policy_eval_failed"),
                trigger_reason=trigger_reason,
                webhook_event=context.webhook_event,
            ),
            actions=(
                build_jira_enqueue_skipped_notification_action(
                    context=context,
                    reason="policy_eval_failed",
                    extra_detail=precheck_decision.policy_error,
                ),
            ),
        )
    if gate_block is not None:
        logger.info(
            "jira_webhook_not_started request_id=%s tenant_id=%s issue_key=%s reason=%s",
            context.request_id,
            context.tenant_id,
            context.issue_key,
            gate_block.reason,
        )
        return JiraRunPlan(
            content=jira_webhook_response_fn(
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
            ),
            actions=(
                build_jira_enqueue_skipped_notification_action(
                    context=context,
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
                ),
            ),
        )

    enqueue_result = plan_jira_enqueue(
        session=session,
        context=context,
        issue_description=resolved_issue_description,
        precheck_decision=precheck_decision,
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
        return JiraRunPlan(
            content=jira_webhook_response_fn(
                context,
                enqueued=False,
                reason=enqueue_result.reason,
                guidance=enqueue_reason_guidance(enqueue_result.reason),
                run_id=enqueue_result.run.run_id,
                trigger_reason=trigger_reason,
                command=context.comment_command,
                webhook_event=context.webhook_event,
            ),
            actions=(
                build_jira_enqueue_skipped_notification_action(
                    context=context,
                    reason=enqueue_result.reason,
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


def build_jira_enqueue_skipped_notification_action(
    *,
    context,
    reason: str,
    extra_detail: str | None = None,
) -> DiscordTenantNotificationAction:
    detail = f" ({extra_detail})" if extra_detail else ""
    guidance = enqueue_reason_guidance(reason)
    message = (
        f"Jira webhook did not queue a run for `{context.issue_key}`.\n"
        f"Reason: `{reason}`{detail}\n"
        f"Guidance: {guidance}\n"
        f"Status: `{context.issue_status or 'unknown'}`"
    )
    return DiscordTenantNotificationAction(
        tenant_id=context.tenant_id,
        project_id=context.project.project_id,
        message=message,
    )
