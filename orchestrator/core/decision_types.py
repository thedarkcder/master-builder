from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal

from orchestrator.core.communications.enqueue_reason_contract import enqueue_reason_guidance
from orchestrator.core.decision_gate import DecisionGateResult
from orchestrator.core.pre_run_check import PreRunCheckResult

DecisionSource = Literal[
    "jira_webhook",
    "discord_run",
    "discord_retry",
    "discord_reply",
    "admin_rerun",
    "cli_run",
    "github_pr_remediation",
    "worker_execution",
]

_BLOCKING_PRECHECK_OUTCOMES = {
    "decision_gate_required",
    "gtd_required",
    "missing_ready_label",
}


def blocking_reason_for_precheck(pre_check: object) -> str | None:
    outcome = str(getattr(pre_check, "outcome", "") or "").strip()
    if outcome in _BLOCKING_PRECHECK_OUTCOMES:
        return outcome
    return None


@dataclass(frozen=True)
class DecisionLabelAction:
    label: str
    action: Literal["add"]
    reason: Literal["ready_label_missing", "required_worker_label_missing"]


@dataclass(frozen=True)
class IngressDecision:
    source: DecisionSource
    pre_check: object | None
    block_reason: str | None
    guidance: str | None
    policy_error: str | None
    label_actions: tuple[DecisionLabelAction, ...]

    def with_applied_labels(self, applied_labels: list[str]) -> IngressDecision:
        if self.pre_check is None or not applied_labels or not isinstance(self.pre_check, PreRunCheckResult):
            return self
        normalized_applied = {str(label).strip().casefold() for label in applied_labels if str(label).strip()}
        if not normalized_applied:
            return self

        updated_pre_check = self.pre_check
        if (
            updated_pre_check.ready_label
            and updated_pre_check.ready_label.casefold() in normalized_applied
            and updated_pre_check.ready_label_missing
        ):
            updated_pre_check = updated_pre_check.with_ready_label_present()
        if (
            updated_pre_check.required_worker_label
            and updated_pre_check.required_worker_label.casefold() in normalized_applied
            and not updated_pre_check.required_worker_label_present
        ):
            updated_pre_check = replace(updated_pre_check, required_worker_label_present=True)

        updated_block_reason = blocking_reason_for_precheck(updated_pre_check)
        return IngressDecision(
            source=self.source,
            pre_check=updated_pre_check,
            block_reason=updated_block_reason,
            guidance=enqueue_reason_guidance(updated_block_reason) if updated_block_reason else None,
            policy_error=self.policy_error,
            label_actions=self.label_actions,
        )


@dataclass(frozen=True)
class WorkerDecision:
    allowed: bool
    decision_gate: DecisionGateResult | None
    configuration_error: str | None
    block_reason: str | None = None
    classification: str | None = None
    pre_check: PreRunCheckResult | None = None


@dataclass(frozen=True)
class DecisionEventInput:
    source: DecisionSource
    event_type: str
    idempotency_key: str | None
    issue_key: str
    issue_summary: str | None
    issue_description: str | None
    issue_labels: list[str] | None
    occurred_at: datetime | None = None


@dataclass(frozen=True)
class DecisionEngineResult:
    decision: IngressDecision
    issue_labels: list[str]
    classification: str
    missing_slots: list[str]
    auto_resolved_slots: list[str]
    case_id: str
    case_state: str
    cycle_id: str | None
    outbox_effect_ids: tuple[str, ...]
    duplicate_event: bool
