from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import quote_plus

from fastapi import HTTPException
from sqlalchemy.orm import Session

from orchestrator.api.jira_oauth.connection_service import tenant_jira_oauth_context
from orchestrator.api.webhooks.jira_admission_flow import (
    build_backlog_pre_run_check,
    evaluate_precheck_decision_with_labels,
)
from orchestrator.api.webhooks.jira_webhook_types import JiraWebhookContext, jira_webhook_response
from orchestrator.core.communications import DiscordTenantNotificationAction, TransportAction
from orchestrator.core.communications.execution_admission_format import present_jira_admission
from orchestrator.core.communications.jira_enqueue_presentation import (
    format_backlog_pre_run_check_message,
    format_jira_enqueue_skipped_message_from_admission,
)
from orchestrator.core.decision_state_machine import (
    ExecutionAdmissionReason,
    build_execution_admission_block,
)
from orchestrator.tools.jira_oauth import JiraOAuthError
from orchestrator.tools.jira_oauth_http import JiraOAuthHttpClient

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


def _issues_payload_contains_issue(*, payload: object, issue_key: str) -> bool:
    if not isinstance(payload, dict):
        return False
    issues = payload.get("issues")
    if not isinstance(issues, list):
        return False
    normalized_issue_key = issue_key.strip().upper()
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        candidate_key = str(issue.get("key") or "").strip().upper()
        if candidate_key == normalized_issue_key:
            return True
    return False


def _fetch_issue_board_location(
    *,
    context: JiraWebhookContext,
    session: Session,
    settings,  # noqa: ANN001
    board_id: int,
) -> tuple[str, str | None]:
    try:
        oauth_context = tenant_jira_oauth_context(
            session=session,
            tenant=context.tenant,
            settings=settings,
        )
    except HTTPException as exc:
        return "error", str(exc.detail)
    issue_jql = quote_plus(f'key = "{context.issue_key}"')
    base_url = f"https://api.atlassian.com/ex/jira/{oauth_context.connection.cloud_id}/rest/agile/1.0"
    backlog_url = f"{base_url}/board/{board_id}/backlog?jql={issue_jql}&maxResults=1"
    board_url = f"{base_url}/board/{board_id}/issue?jql={issue_jql}&maxResults=1"
    http_client = JiraOAuthHttpClient()
    backlog_error_detail: str | None = None
    try:
        backlog_payload = http_client.get_json(
            url=backlog_url,
            access_token=oauth_context.access_token,
        )
        if _issues_payload_contains_issue(payload=backlog_payload, issue_key=context.issue_key):
            return "backlog", None
    except (JiraOAuthError, ValueError) as exc:
        backlog_error_detail = f"backlog lookup failed: {exc}"
    try:
        board_payload = http_client.get_json(
            url=board_url,
            access_token=oauth_context.access_token,
        )
        if _issues_payload_contains_issue(payload=board_payload, issue_key=context.issue_key):
            return "board", None
    except (JiraOAuthError, ValueError) as exc:
        board_error_detail = f"board lookup failed: {exc}"
        if backlog_error_detail:
            return "error", f"{backlog_error_detail}; {board_error_detail}"
        return "error", board_error_detail
    if backlog_error_detail:
        return "not_on_board", backlog_error_detail
    return "not_on_board", None


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
    location, detail = _fetch_issue_board_location(
        context=context,
        session=session,
        settings=settings,
        board_id=board_id,
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
        pre_run_check = build_backlog_pre_run_check(
            context=context,
            decision_result=decision_result,
        )
        if context.webhook_event == "issue_created":
            actions = (_build_backlog_pre_run_check_notification_action(
                context=context,
                board_id=board_id,
                pre_run_check=pre_run_check,
            ),)
        else:
            actions = ()
        logger.info(
            "jira_webhook_backlog_pre_run_check request_id=%s tenant_id=%s issue_key=%s board_id=%s outcome=%s decision_gate_triggered=%s",
            context.request_id,
            context.tenant_id,
            context.issue_key,
            board_id,
            pre_run_check.get("outcome"),
            pre_run_check.get("decision_gate_triggered"),
        )
        return JiraBoardGatePlan(
            response=jira_webhook_response(
                context,
                enqueued=False,
                board_id=board_id,
                webhook_event=context.webhook_event,
                detail=detail,
                pre_run_check=pre_run_check,
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
                admission=admission,
                extra_detail=f"board_id={board_id}" if detail is None else f"board_id={board_id}; detail={detail}",
            ),
        ),
    )


def _build_enqueue_skipped_notification_action(
    *,
    context: JiraWebhookContext,
    admission,
    extra_detail: str | None,
) -> DiscordTenantNotificationAction:
    message = format_jira_enqueue_skipped_message_from_admission(
        issue_key=context.issue_key,
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
    pre_run_check: dict[str, object],
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
