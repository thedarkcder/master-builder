from __future__ import annotations

from dataclasses import dataclass

from orchestrator.core.decision_gate import DecisionGateResult, evaluate_decision_gate
from orchestrator.core.worker_capabilities import (
    infer_required_worker_capability,
    worker_label_for_capability,
)


@dataclass(frozen=True)
class PreRunCheckResult:
    outcome: str
    ready_label: str | None
    ready_label_present: bool
    required_worker_capability: str
    required_worker_label: str
    required_worker_label_present: bool
    decision_gate: DecisionGateResult

    @property
    def decision_gate_triggered(self) -> bool:
        return self.decision_gate.triggered

    @property
    def decision_gate_reason(self) -> str:
        return self.decision_gate.reason


def evaluate_pre_run_check(
    *,
    issue_summary: str | None,
    issue_description: str | None,
    issue_labels: list[str] | None,
    ready_label: str | None,
) -> PreRunCheckResult:
    normalized_labels = {str(label).strip().casefold() for label in issue_labels or []}
    normalized_ready_label = str(ready_label or "").strip() or None
    ready_label_present = bool(
        normalized_ready_label and normalized_ready_label.casefold() in normalized_labels
    )
    required_worker_capability = infer_required_worker_capability(
        issue_summary=issue_summary,
        issue_description=issue_description,
        issue_labels=issue_labels,
    )
    required_worker_label = worker_label_for_capability(required_worker_capability)
    required_worker_label_present = required_worker_label.casefold() in normalized_labels

    decision_gate = evaluate_decision_gate(
        issue_summary=issue_summary,
        issue_description=issue_description,
    )
    if decision_gate.triggered:
        outcome = "decision_gate_required"
    elif normalized_ready_label is None or ready_label_present:
        outcome = "ready_for_agent"
    else:
        outcome = "missing_ready_label"

    return PreRunCheckResult(
        outcome=outcome,
        ready_label=normalized_ready_label,
        ready_label_present=ready_label_present,
        required_worker_capability=required_worker_capability,
        required_worker_label=required_worker_label,
        required_worker_label_present=required_worker_label_present,
        decision_gate=decision_gate,
    )
