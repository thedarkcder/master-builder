from __future__ import annotations

from dataclasses import dataclass

from orchestrator.core.decision_gate import DecisionGateResult, evaluate_decision_gate
from orchestrator.core.gtd import GoodToDoValidationResult, validate_good_to_do
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
    gtd: GoodToDoValidationResult

    @property
    def decision_gate_triggered(self) -> bool:
        return self.decision_gate.triggered

    @property
    def decision_gate_reason(self) -> str:
        return self.decision_gate.reason

    @property
    def gtd_valid(self) -> bool:
        return self.gtd.valid

    @property
    def gtd_missing_criteria(self) -> tuple[str, ...]:
        return self.gtd.missing_criteria

    @property
    def gtd_clarification_questions(self) -> tuple[str, ...]:
        return self.gtd.clarification_questions


def evaluate_pre_run_check(
    *,
    tenant_id: str | None = None,
    project_id: str | None = None,
    issue_key: str | None = None,
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
        tenant_id=tenant_id,
        project_id=project_id,
        issue_key=issue_key,
    )
    required_worker_label = worker_label_for_capability(required_worker_capability)
    required_worker_label_present = required_worker_label.casefold() in normalized_labels

    decision_gate = evaluate_decision_gate(
        issue_summary=issue_summary,
        issue_description=issue_description,
        tenant_id=tenant_id,
        project_id=project_id,
        issue_key=issue_key,
    )
    gtd = validate_good_to_do(
        issue_summary=(issue_summary or "").strip(),
        issue_description=(issue_description or "").strip(),
        tenant_id=tenant_id,
        project_id=project_id,
        issue_key=issue_key,
    )

    if decision_gate.triggered:
        outcome = "decision_gate_required"
    elif not gtd.valid:
        outcome = "gtd_required"
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
        gtd=gtd,
    )
