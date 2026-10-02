from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

from orchestrator.api.webhooks.jira_admission_flow import (
    evaluate_precheck_decision_with_labels,
)
from orchestrator.api.webhooks.jira_board_location import (
    resolve_project_issue_board_location,
)
from orchestrator.api.webhooks.jira_webhook_types import (
    JiraWebhookContext,
    jira_webhook_response,
)
from orchestrator.core.communications import (
    DiscordTenantNotificationAction,
    TransportAction,
)
from orchestrator.core.communications.execution_admission_format import (
    present_jira_admission,
)
from orchestrator.core.communications.jira_enqueue_presentation import (
    BacklogPreRunCheckPresentation,
    build_backlog_pre_run_check_presentation,
    format_backlog_pre_run_check_message,
    format_jira_enqueue_skipped_message,
)
from orchestrator.core.decision.types import tenant_ready_label
from orchestrator.core.decision.state_machine import (
    ExecutionAdmissionReason,
    build_execution_admission_block,
)
from orchestrator.core.integrations.atlassian.links import tenant_jira_issue_url

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class JiraBoardGatePlan:
    response: dict
    actions: tuple[TransportAction, ...] = ()


def stage_handle_backlog_followup_issue_created(
    *,
    context: JiraWebhookContext,
) -> dict | None:
    if context.webhook_event != "issue_created":
        return None
    normalized_labels = {
        str(label).strip().casefold() for label in context.issue_labels
    }
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


def stage_handle_run_board_gate(
    *,
    context: JiraWebhookContext,
    session: Session,
    settings,  # noqa: ANN001
) -> JiraBoardGatePlan | None:
    raw_board_id = None
    if context.project is not None:
        raw_board_id = (context.project.policy_overrides or {}).get("run_board_id")
    if raw_board_id is None:
        return None
    try:
        board_id = int(raw_board_id)
    except (TypeError, ValueError):
        board_id = 0
    if board_id <= 0:
        logger.info(
            "jira_webhook_not_started request_id=%s tenant_id=%s issue_key=%s reason=board_gate_unconfigured board_id=%s",
            context.request_id,
            context.tenant_id,
            context.issue_key,
            raw_board_id,
        )
        admission = build_execution_admission_block(
            reason=ExecutionAdmissionReason.BOARD_GATE_UNCONFIGURED,
        )
        admission_presentation = present_jira_admission(admission=admission)
        return JiraBoardGatePlan(
            response=jira_webhook_response(
                context,
                enqueued=False,
                board_id=raw_board_id,
                webhook_event=context.webhook_event,
                **admission_presentation.response_fields,
            )
        )
    location, detail = resolve_project_issue_board_location(
        context=context,
        session=session,
        settings=settings,
    )
    if location == "board":
        return None
    if location == "backlog":
        reason = ExecutionAdmissionReason.ISSUE_IN_BACKLOG
        admission = build_execution_admission_block(reason=reason)
        admission_presentation = present_jira_admission(admission=admission)
        decision_result = evaluate_precheck_decision_with_labels(
            context=context,
            session=session,
            settings=settings,
        )
        pre_run_check = build_backlog_pre_run_check_presentation(
            decision_result=decision_result,
            ready_label=tenant_ready_label(context.tenant),
        )
        if context.webhook_event == "issue_created":
            actions = (
                _build_backlog_pre_run_check_notification_action(
                    context=context,
                    board_id=board_id,
                    pre_run_check=pre_run_check,
                ),
            )
        else:
            actions = ()
        logger.info(
            "jira_webhook_backlog_pre_run_check request_id=%s tenant_id=%s issue_key=%s board_id=%s outcome=%s decision_gate_triggered=%s",
            context.request_id,
            context.tenant_id,
            context.issue_key,
            board_id,
            pre_run_check.outcome.value,
            bool(pre_run_check.decision_gate_reason),
        )
        return JiraBoardGatePlan(
            response=jira_webhook_response(
                context,
                enqueued=False,
                board_id=board_id,
                webhook_event=context.webhook_event,
                detail=detail,
                pre_run_check=pre_run_check.to_payload(),
                **admission_presentation.response_fields,
            ),
            actions=actions,
        )
    reason = (
        ExecutionAdmissionReason.ISSUE_NOT_ON_BOARD
        if location == "not_on_board"
        else ExecutionAdmissionReason.BOARD_GATE_CHECK_FAILED
    )
    admission = build_execution_admission_block(reason=reason)
    admission_presentation = present_jira_admission(admission=admission)
    logger.info(
        "jira_webhook_not_started request_id=%s tenant_id=%s issue_key=%s reason=%s board_id=%s detail=%s",
        context.request_id,
        context.tenant_id,
        context.issue_key,
        admission.reason_code,
        board_id,
        detail,
    )
    return JiraBoardGatePlan(
        response=jira_webhook_response(
            context,
            enqueued=False,
            board_id=board_id,
            webhook_event=context.webhook_event,
            detail=detail,
            **admission_presentation.response_fields,
        ),
        actions=(
            _build_enqueue_skipped_notification_action(
                context=context,
                session=session,
                admission=admission,
                extra_detail=f"board_id={board_id}"
                if detail is None
                else f"board_id={board_id}; detail={detail}",
            ),
        ),
    )


def _build_enqueue_skipped_notification_action(
    *,
    context: JiraWebhookContext,
    session: Session,
    admission,
    extra_detail: str | None,
) -> DiscordTenantNotificationAction:
    message = format_jira_enqueue_skipped_message(
        issue_key=context.issue_key,
        issue_url=tenant_jira_issue_url(
            session=session, tenant=context.tenant, issue_key=context.issue_key
        ),
        issue_status=context.issue_status,
        admission=admission,
        extra_detail=extra_detail,
    )
    return DiscordTenantNotificationAction(
        tenant_id=context.tenant_id,
        project_id=context.project.project_id if context.project is not None else None,
        message=message,
    )


def _build_backlog_pre_run_check_notification_action(
    *,
    context: JiraWebhookContext,
    board_id: int,
    pre_run_check: BacklogPreRunCheckPresentation,
) -> DiscordTenantNotificationAction:
    return DiscordTenantNotificationAction(
        tenant_id=context.tenant_id,
        project_id=context.project.project_id if context.project is not None else None,
        message=format_backlog_pre_run_check_message(
            issue_key=context.issue_key,
            board_id=board_id,
            issue_status=context.issue_status,
            pre_run_check=pre_run_check,
        ),
    )
