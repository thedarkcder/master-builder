from __future__ import annotations

from dataclasses import dataclass

from orchestrator.api.webhooks.contracts import extract_status_transition
from orchestrator.api.webhooks.jira_trigger_policy import (
    resolve_decision_gate_cooldown_block,
    resolve_jira_trigger_decision,
    resolve_retry_source,
)
from orchestrator.api.webhooks.jira_webhook_types import DECISION_GATE_COOLDOWN


@dataclass(frozen=True)
class JiraTriggerEvaluation:
    trigger_reason: str
    from_status: str | None
    to_status: str | None
    retry_resolution: object
    cooldown_block: object | None
    trigger_mode_skip_reason: str | None


def evaluate_jira_trigger_state(
    *,
    context,
    session,
    ready_trigger_mode: str,
    now,
) -> JiraTriggerEvaluation:  # noqa: ANN001
    from_status, to_status = extract_status_transition(context.payload)
    trigger_decision = resolve_jira_trigger_decision(
        comment_command=context.comment_command,
        webhook_event=context.webhook_event,
        from_status=from_status,
        to_status=to_status,
        ready_trigger_mode=ready_trigger_mode,
    )
    cooldown_block = resolve_decision_gate_cooldown_block(
        session=session,
        tenant_id=context.tenant_id,
        issue_key=context.issue_key,
        cooldown_window=DECISION_GATE_COOLDOWN,
        now=now,
    )
    retry_resolution = resolve_retry_source(
        session=session,
        tenant_id=context.tenant_id,
        issue_key=context.issue_key,
        comment_command=context.comment_command,
        fallback_issue_description=context.issue_description,
    )
    return JiraTriggerEvaluation(
        trigger_reason=trigger_decision.trigger_reason,
        from_status=from_status,
        to_status=to_status,
        retry_resolution=retry_resolution,
        cooldown_block=cooldown_block,
        trigger_mode_skip_reason=trigger_decision.trigger_mode_skip_reason,
    )
