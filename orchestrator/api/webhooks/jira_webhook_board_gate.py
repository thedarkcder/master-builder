from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import quote_plus

from fastapi import HTTPException
from sqlalchemy.orm import Session

from orchestrator.api.jira_oauth.connection_service import tenant_jira_oauth_context
from orchestrator.api.webhooks.jira_webhook_precheck import (
    build_backlog_pre_run_check,
    evaluate_precheck_decision_with_labels,
)
from orchestrator.api.webhooks.jira_webhook_types import JiraWebhookContext, jira_webhook_response
from orchestrator.core.communications import DiscordTenantNotificationAction, TransportAction
from orchestrator.core.communications.enqueue_reason_contract import enqueue_reason_guidance
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
        return JiraBoardGatePlan(
            response=jira_webhook_response(
                context,
                enqueued=False,
                reason="board_gate_unconfigured",
                guidance=enqueue_reason_guidance("board_gate_unconfigured"),
                board_id=raw_board_id,
                webhook_event=context.webhook_event,
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
        reason = "issue_in_backlog"
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
                reason=reason,
                guidance=enqueue_reason_guidance(reason),
                board_id=board_id,
                webhook_event=context.webhook_event,
                detail=detail,
                pre_run_check=pre_run_check,
            ),
            actions=actions,
        )
    reason = "issue_not_on_board" if location == "not_on_board" else "board_gate_check_failed"
    logger.info(
        "jira_webhook_not_started request_id=%s tenant_id=%s issue_key=%s reason=%s board_id=%s detail=%s",
        context.request_id,
        context.tenant_id,
        context.issue_key,
        reason,
        board_id,
        detail,
    )
    return JiraBoardGatePlan(
        response=jira_webhook_response(
            context,
            enqueued=False,
            reason=reason,
            guidance=enqueue_reason_guidance(reason),
            board_id=board_id,
            webhook_event=context.webhook_event,
            detail=detail,
        ),
        actions=(
            _build_enqueue_skipped_notification_action(
                context=context,
                reason=reason,
                extra_detail=f"board_id={board_id}" if detail is None else f"board_id={board_id}; detail={detail}",
            ),
        ),
    )


def _build_enqueue_skipped_notification_action(
    *,
    context: JiraWebhookContext,
    reason: str,
    extra_detail: str | None,
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
        project_id=context.project.project_id if context.project is not None else None,
        message=message,
    )


def _build_backlog_pre_run_check_notification_action(
    *,
    context: JiraWebhookContext,
    board_id: int,
    pre_run_check: dict[str, object],
) -> DiscordTenantNotificationAction:
    outcome = str(pre_run_check.get("outcome") or "").strip()
    ready_label = pre_run_check.get("ready_label")
    decision_gate_reason = pre_run_check.get("decision_gate_reason")
    normalized_decision_gate_reason = (
        " ".join(str(decision_gate_reason).strip().split())[:240]
        if isinstance(decision_gate_reason, str) and str(decision_gate_reason).strip()
        else None
    )
    gtd_missing_criteria = [
        str(item).strip()
        for item in (pre_run_check.get("gtd_missing_criteria") or [])
        if str(item).strip()
    ]
    lines = [
        f"New issue `{context.issue_key}` was added to the backlog on board `{board_id}`.",
        "Run was not started (backlog-only event).",
    ]
    if context.issue_status:
        lines.append(f"Issue status: `{context.issue_status}`")
    if outcome == "ready_for_agent":
        if isinstance(ready_label, str) and ready_label.strip():
            lines.append(f"Pre-run check: labeled `{ready_label.strip()}` and ready for agent.")
        else:
            lines.append("Pre-run check: ready for agent.")
    elif outcome == "decision_gate_required":
        lines.append("Pre-run check: Decision Gate required before execution.")
        if normalized_decision_gate_reason:
            lines.append(f"Decision Gate reason: {normalized_decision_gate_reason}")
    elif outcome == "gtd_required":
        lines.append("Pre-run check: Good To Do details are incomplete.")
        if gtd_missing_criteria:
            lines.append("Missing GTD criteria: " + ", ".join(gtd_missing_criteria))
    elif outcome == "missing_ready_label":
        if isinstance(ready_label, str) and ready_label.strip():
            lines.append(f"Pre-run check: missing ready label `{ready_label.strip()}`.")
        else:
            lines.append("Pre-run check: missing ready label.")
    required_worker_label = str(pre_run_check.get("required_worker_label") or "").strip()
    if required_worker_label:
        lines.append(f"Required worker capability: `{required_worker_label}`.")
    return DiscordTenantNotificationAction(
        tenant_id=context.tenant_id,
        project_id=context.project.project_id if context.project is not None else None,
        message="\n".join(lines),
    )
