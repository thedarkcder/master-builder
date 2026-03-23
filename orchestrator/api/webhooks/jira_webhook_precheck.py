from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from orchestrator.api.jira_oauth.connection_service import tenant_jira_oauth_context
from orchestrator.api.webhooks.contracts import post_jira_comment
from orchestrator.api.webhooks.jira_webhook_types import JiraWebhookContext, TODO_STATUS
from orchestrator.core.communications.enqueue_reason_contract import enqueue_reason_guidance
from orchestrator.core.decision_clarification_service import evaluate_issue_clarification_state
from orchestrator.core.decision_engine import DecisionEngineResult, DecisionEventInput
from orchestrator.core.discord.notifications import send_tenant_discord_message
from orchestrator.core.pre_run_check import evaluate_pre_run_check
from orchestrator.api.discord.shared.state import normalize_status_name

logger = logging.getLogger(__name__)


def is_todo_status(status_name: str) -> bool:
    return normalize_status_name(status_name) == TODO_STATUS


def resolve_ready_trigger_mode_for_tenant(tenant) -> str:  # noqa: ANN001
    raw_mode = tenant.jira_config.get("ready_trigger_mode")
    if isinstance(raw_mode, str):
        normalized_mode = raw_mode.strip().lower()
        if normalized_mode in {"status_recheck", "transition_only"}:
            return normalized_mode
    return "status_recheck"


def notify_jira_enqueue_skipped(
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


def normalize_backlog_pre_run_check_text(raw_value: str | None, *, max_chars: int = 240) -> str | None:
    if not isinstance(raw_value, str):
        return None
    normalized = " ".join(raw_value.strip().split())
    if not normalized:
        return None
    if len(normalized) > max_chars:
        return f"{normalized[: max_chars - 1].rstrip()}..."
    return normalized


def resolve_ready_label_for_tenant(tenant) -> str | None:  # noqa: ANN001
    raw_ready_label = (tenant.jira_config or {}).get("ready_label")
    if not isinstance(raw_ready_label, str):
        return None
    ready_label = raw_ready_label.strip()
    if not ready_label:
        return None
    return ready_label


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
            "required_worker_capability": "linux",
            "required_worker_label": "worker:linux",
            "required_worker_label_present": False,
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


def notify_backlog_pre_run_check(
    *,
    context: JiraWebhookContext,
    session: Session,
    settings,  # noqa: ANN001
    board_id: int,
    pre_run_check: dict[str, object],
) -> None:
    outcome = str(pre_run_check.get("outcome") or "").strip()
    ready_label = pre_run_check.get("ready_label")
    decision_gate_reason = normalize_backlog_pre_run_check_text(
        pre_run_check.get("decision_gate_reason") if isinstance(pre_run_check.get("decision_gate_reason"), str) else None
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
        if decision_gate_reason:
            lines.append(f"Decision Gate reason: {decision_gate_reason}")
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
    send_tenant_discord_message(
        session=session,
        tenant=context.tenant,
        project=context.project,
        message="\n".join(lines),
        settings=settings,
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
    return result
