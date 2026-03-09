from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable, Literal

from orchestrator.core.communications.enqueue_reason_contract import enqueue_reason_guidance
from orchestrator.core.decision_gate import DecisionGateResult
from orchestrator.core.pre_run_check import PreRunCheckResult
from orchestrator.core.runs import is_ready_for_agent_precheck, resolve_precheck_outcome_for_enqueue

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

_READY_FOR_AGENT_OVERRIDE_SOURCES = {
    "admin_rerun",
    "cli_run",
    "github_pr_remediation",
}


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

        updated_block_reason = _blocking_reason_for_precheck(updated_pre_check)
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


def evaluate_ingress_precheck(
    *,
    source: DecisionSource,
    tenant_id: str | None,
    project_id: str | None,
    issue_key: str | None,
    issue_summary: str | None,
    issue_description: str | None,
    issue_labels: list[str] | None,
    ready_label: str | None,
    evaluate_pre_run_check_fn: Callable[..., PreRunCheckResult],
) -> IngressDecision:
    try:
        pre_check = evaluate_pre_run_check_fn(
            tenant_id=tenant_id,
            project_id=project_id,
            issue_key=issue_key,
            issue_summary=issue_summary,
            issue_description=issue_description,
            issue_labels=issue_labels,
            ready_label=ready_label,
        )
    except Exception as exc:  # noqa: BLE001
        return IngressDecision(
            source=source,
            pre_check=None,
            block_reason="policy_eval_failed",
            guidance=enqueue_reason_guidance("policy_eval_failed"),
            policy_error=str(exc),
            label_actions=(),
        )

    actions = derive_label_actions(pre_check)
    block_reason = _blocking_reason_for_precheck(pre_check)
    return IngressDecision(
        source=source,
        pre_check=pre_check,
        block_reason=block_reason,
        guidance=enqueue_reason_guidance(block_reason) if block_reason else None,
        policy_error=None,
        label_actions=actions,
    )


def derive_label_actions(pre_check: object) -> tuple[DecisionLabelAction, ...]:
    actions: list[DecisionLabelAction] = []
    ready_label_missing = bool(getattr(pre_check, "ready_label_missing", False))
    ready_label = str(getattr(pre_check, "ready_label", "") or "").strip()
    if ready_label_missing and ready_label:
        actions.append(
            DecisionLabelAction(
                label=ready_label,
                action="add",
                reason="ready_label_missing",
            )
        )
    required_worker_label = str(getattr(pre_check, "required_worker_label", "") or "").strip()
    required_worker_label_present = bool(getattr(pre_check, "required_worker_label_present", True))
    if not required_worker_label_present and required_worker_label:
        actions.append(
            DecisionLabelAction(
                label=required_worker_label,
                action="add",
                reason="required_worker_label_missing",
            )
        )
    deduped: list[DecisionLabelAction] = []
    seen: set[str] = set()
    for action in actions:
        key = action.label.casefold()
        if key in seen:
            continue
        deduped.append(action)
        seen.add(key)
    return tuple(deduped)


def resolve_enqueue_precheck_outcome(
    *,
    source: DecisionSource,
    precheck_outcome: str | None = None,
    precheck_source_plan: object | None = None,
) -> str | None:
    normalized_outcome = resolve_precheck_outcome_for_enqueue(
        precheck_outcome=precheck_outcome,
        precheck_source_plan=precheck_source_plan,
    )
    if normalized_outcome is not None:
        return normalized_outcome
    if source in _READY_FOR_AGENT_OVERRIDE_SOURCES:
        return "ready_for_agent"
    return None


def evaluate_worker_decision(
    *,
    run_plan: object | None,
    tenant_id: str | None,
    project_id: str | None,
    issue_key: str | None,
    run_id: str | None,
    issue_summary: str | None,
    issue_description: str | None,
    evaluate_decision_gate_fn: Callable[..., DecisionGateResult],
) -> WorkerDecision:
    if is_ready_for_agent_precheck(run_plan):
        return WorkerDecision(allowed=True, decision_gate=None, configuration_error=None)
    try:
        decision_gate = evaluate_decision_gate_fn(
            tenant_id=tenant_id,
            project_id=project_id,
            issue_key=issue_key,
            run_id=run_id,
            issue_summary=issue_summary,
            issue_description=issue_description,
        )
    except (FileNotFoundError, ValueError) as exc:
        return WorkerDecision(
            allowed=False,
            decision_gate=None,
            configuration_error=f"Decision Gate configuration error: {exc}",
        )

    if decision_gate.triggered:
        return WorkerDecision(allowed=False, decision_gate=decision_gate, configuration_error=None)
    return WorkerDecision(allowed=True, decision_gate=decision_gate, configuration_error=None)


def _blocking_reason_for_precheck(pre_check: object) -> str | None:
    outcome = str(getattr(pre_check, "outcome", "") or "").strip()
    if outcome in _BLOCKING_PRECHECK_OUTCOMES:
        return outcome
    return None
