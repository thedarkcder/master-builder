from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
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
    "execution_blocked",
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


class DecisionClassification(str, Enum):
    CLEAR = "clear"
    DECISION_GATE = "decision_gate"
    GTD = "gtd"
    BOTH = "both"


class DecisionQuestionStatus(str, Enum):
    OPEN = "open"
    ANSWERED = "answered"
    ACCEPTED = "accepted"


class ExecutionGateState(str, Enum):
    ALLOW_EXECUTION = "allow_execution"
    BLOCK_DECISION = "block_decision"
    BLOCK_READY_LABEL = "block_ready_label"
    POLICY_ERROR = "policy_error"


@dataclass(frozen=True)
class ExecutionGateReason:
    reason_code: str
    guidance: str
    detail: str | None = None
    ready_label: str | None = None


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
    execution_gate_state: ExecutionGateState = ExecutionGateState.ALLOW_EXECUTION
    execution_gate_reason: ExecutionGateReason | None = None


def resolve_execution_gate_state(
    *,
    decision: IngressDecision,
    classification: str,
) -> tuple[ExecutionGateState, ExecutionGateReason | None]:
    if decision.policy_error or decision.pre_check is None:
        return (
            ExecutionGateState.POLICY_ERROR,
            ExecutionGateReason(
                reason_code="policy_eval_failed",
                guidance=enqueue_reason_guidance("policy_eval_failed"),
                detail=str(decision.policy_error or "").strip() or None,
            ),
        )

    block_reason = str(decision.block_reason or "").strip()
    if block_reason == "missing_ready_label":
        ready_label = str(getattr(decision.pre_check, "ready_label", "") or "").strip() or None
        guidance = (
            f"{enqueue_reason_guidance('missing_ready_label')} ({ready_label})"
            if ready_label
            else enqueue_reason_guidance("missing_ready_label")
        )
        return (
            ExecutionGateState.BLOCK_READY_LABEL,
            ExecutionGateReason(
                reason_code="missing_ready_label",
                guidance=guidance,
                ready_label=ready_label,
            ),
        )

    if block_reason in {"decision_gate_required", "gtd_required", "execution_blocked"} or classification in {"decision_gate", "gtd", "both"}:
        detail = str(getattr(decision.pre_check, "decision_gate_reason", "") or "").strip() or None
        return (
            ExecutionGateState.BLOCK_DECISION,
            ExecutionGateReason(
                reason_code=block_reason or "decision_gate_required",
                guidance=enqueue_reason_guidance(block_reason or "decision_gate_required"),
                detail=detail,
            ),
        )

    return (ExecutionGateState.ALLOW_EXECUTION, None)
