from __future__ import annotations

from dataclasses import dataclass

from orchestrator.core.decision_gate import DecisionGateResult
from orchestrator.core.gtd import GoodToDoValidationResult
from orchestrator.core.precheck_policy import evaluate_precheck_policy
from orchestrator.core.worker_capability_normalization import WorkerCapability
from orchestrator.core.worker_capabilities import (
    infer_required_worker_capability,
    parse_worker_capability_labels,
    WorkerLabelParseResult,
    worker_label_for_capability,
)

_PM_PARENT_LABELS = {"pm-parent"}
_SYNC_BLOCKING_LABELS = {"sync-stale", "sync-blocked"}


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

    @property
    def ready_label_missing(self) -> bool:
        return bool(self.ready_label and not self.ready_label_present)

    def with_ready_label_present(self) -> PreRunCheckResult:
        if not self.ready_label_missing:
            return self
        outcome = self.outcome
        if outcome == "missing_ready_label":
            outcome = "ready_for_agent"
        return PreRunCheckResult(
            outcome=outcome,
            ready_label=self.ready_label,
            ready_label_present=True,
            required_worker_capability=self.required_worker_capability,
            required_worker_label=self.required_worker_label,
            required_worker_label_present=self.required_worker_label_present,
            decision_gate=self.decision_gate,
            gtd=self.gtd,
        )


def evaluate_execution_readiness_only(
    *,
    tenant_id: str | None = None,
    project_id: str | None = None,
    issue_key: str | None = None,
    issue_summary: str | None,
    issue_description: str | None,
    issue_labels: list[str] | None,
    ready_label: str | None,
) -> PreRunCheckResult:
    ready_for_agent_labels = {
        "ready_for_agent",
        "ready-for-agent",
        "ready for agent",
    }
    normalized_labels = {str(label).strip().casefold() for label in issue_labels or []}
    normalized_ready_label = str(ready_label or "").strip() or None
    ready_label_present = bool(
        normalized_ready_label and normalized_ready_label.casefold() in normalized_labels
    )
    required_worker_capability, invalid_worker_labels, conflicting_worker_capabilities = _resolve_required_worker_capability(
        issue_summary=issue_summary,
        issue_description=issue_description,
        issue_labels=issue_labels,
        tenant_id=tenant_id,
        project_id=project_id,
        issue_key=issue_key,
    )
    required_worker_label = worker_label_for_capability(required_worker_capability)
    required_worker_label_present = required_worker_label.casefold() in normalized_labels
    invalid_label_guard = _invalid_worker_label_guard(invalid_worker_labels)
    conflicting_capability_guard = _conflicting_worker_capability_guard(conflicting_worker_capabilities)
    sync_guard = _execution_sync_guard(normalized_labels=normalized_labels)
    gtd_guard = invalid_label_guard or conflicting_capability_guard or sync_guard
    ready_for_agent_label_override = any(label in normalized_labels for label in ready_for_agent_labels)
    outcome = "gtd_required" if gtd_guard is not None else "ready_for_agent"
    if gtd_guard is None and normalized_ready_label is not None and not ready_label_present and not ready_for_agent_label_override:
        outcome = "missing_ready_label"
    return PreRunCheckResult(
        outcome=outcome,
        ready_label=normalized_ready_label,
        ready_label_present=ready_label_present or ready_for_agent_label_override,
        required_worker_capability=required_worker_capability,
        required_worker_label=required_worker_label,
        required_worker_label_present=required_worker_label_present,
        decision_gate=DecisionGateResult(
            triggered=False,
            reason=(
                "Execution blocked by invalid worker capability labels"
                if invalid_label_guard is not None
                else (
                    "Execution blocked by conflicting worker capability labels"
                    if conflicting_capability_guard is not None
                    else (
                        "Execution blocked by Jira parent/child sync policy"
                        if sync_guard is not None
                        else "Decision Gate permanently satisfied"
                    )
                )
            ),
            missing_sections=(),
            questions=(),
            recommendation=(
                "Fix invalid worker capability labels before execution."
                if invalid_label_guard is not None
                else (
                    "Use exactly one worker capability label before execution."
                    if conflicting_capability_guard is not None
                    else (
                        "Update the Jira hierarchy before execution."
                        if sync_guard is not None
                        else "Proceed with execution."
                    )
                )
            ),
            tags=(),
        ),
        gtd=gtd_guard or GoodToDoValidationResult(
            valid=True,
            missing_criteria=(),
            clarification_questions=(),
        ),
    )


def _execution_sync_guard(*, normalized_labels: set[str]) -> GoodToDoValidationResult | None:
    if any(label in normalized_labels for label in _PM_PARENT_LABELS):
        return GoodToDoValidationResult(
            valid=False,
            missing_criteria=("Engineering child ticket required",),
            clarification_questions=("Create or select an engineering child ticket before execution.",),
        )
    if any(label in normalized_labels for label in _SYNC_BLOCKING_LABELS):
        return GoodToDoValidationResult(
            valid=False,
            missing_criteria=("Parent-child sync must be current",),
            clarification_questions=("Refresh this engineering child from the latest parent feature before execution.",),
        )
    return None


def evaluate_pre_run_check(
    *,
    tenant_id: str | None = None,
    project_id: str | None = None,
    issue_key: str | None = None,
    issue_summary: str | None,
    issue_description: str | None,
    recorded_answers: list[dict[str, str]] | None = None,
    issue_labels: list[str] | None,
    ready_label: str | None,
) -> PreRunCheckResult:
    ready_for_agent_labels = {
        "ready_for_agent",
        "ready-for-agent",
        "ready for agent",
    }

    normalized_labels = {str(label).strip().casefold() for label in issue_labels or []}
    normalized_ready_label = str(ready_label or "").strip() or None
    ready_label_present = bool(
        normalized_ready_label and normalized_ready_label.casefold() in normalized_labels
    )
    required_worker_capability, invalid_worker_labels, conflicting_worker_capabilities = _resolve_required_worker_capability(
        issue_summary=issue_summary,
        issue_description=issue_description,
        issue_labels=issue_labels,
        tenant_id=tenant_id,
        project_id=project_id,
        issue_key=issue_key,
    )
    required_worker_label = worker_label_for_capability(required_worker_capability)
    required_worker_label_present = required_worker_label.casefold() in normalized_labels
    invalid_label_guard = _invalid_worker_label_guard(invalid_worker_labels)
    conflicting_capability_guard = _conflicting_worker_capability_guard(conflicting_worker_capabilities)
    if invalid_label_guard is not None:
        return PreRunCheckResult(
            outcome="gtd_required",
            ready_label=normalized_ready_label,
            ready_label_present=ready_label_present,
            required_worker_capability=required_worker_capability,
            required_worker_label=required_worker_label,
            required_worker_label_present=required_worker_label_present,
            decision_gate=DecisionGateResult(
                triggered=False,
                reason="Execution blocked by invalid worker capability labels",
                missing_sections=(),
                questions=(),
                recommendation="Fix invalid worker capability labels before execution.",
                tags=(),
            ),
            gtd=invalid_label_guard,
        )
    if conflicting_capability_guard is not None:
        return PreRunCheckResult(
            outcome="gtd_required",
            ready_label=normalized_ready_label,
            ready_label_present=ready_label_present,
            required_worker_capability=required_worker_capability,
            required_worker_label=required_worker_label,
            required_worker_label_present=required_worker_label_present,
            decision_gate=DecisionGateResult(
                triggered=False,
                reason="Execution blocked by conflicting worker capability labels",
                missing_sections=(),
                questions=(),
                recommendation="Use exactly one worker capability label before execution.",
                tags=(),
            ),
            gtd=conflicting_capability_guard,
        )

    sync_guard = _execution_sync_guard(normalized_labels=normalized_labels)
    if sync_guard is not None:
        return PreRunCheckResult(
            outcome="gtd_required",
            ready_label=normalized_ready_label,
            ready_label_present=ready_label_present,
            required_worker_capability=required_worker_capability,
            required_worker_label=required_worker_label,
            required_worker_label_present=required_worker_label_present,
            decision_gate=DecisionGateResult(
                triggered=False,
                reason="Execution blocked by Jira parent/child sync policy",
                missing_sections=(),
                questions=(),
                recommendation="Update the Jira hierarchy before execution.",
                tags=(),
            ),
            gtd=sync_guard,
        )
    ready_for_agent_label_override = bool(
        any(label in normalized_labels for label in ready_for_agent_labels)
    )
    if ready_for_agent_label_override:
        return PreRunCheckResult(
            outcome="ready_for_agent",
            ready_label=normalized_ready_label,
            ready_label_present=bool(
                normalized_ready_label and normalized_ready_label.casefold() in normalized_labels
            ),
            required_worker_capability=required_worker_capability,
            required_worker_label=required_worker_label,
            required_worker_label_present=required_worker_label_present,
            decision_gate=DecisionGateResult(
                triggered=False,
                reason="Pre-run check bypassed by ready_for_agent label",
                missing_sections=(),
                questions=(),
                recommendation="Proceed with execution.",
                tags=(),
            ),
            gtd=GoodToDoValidationResult(
                valid=True,
                missing_criteria=(),
                clarification_questions=(),
            ),
        )

    precheck_policy = evaluate_precheck_policy(
        issue_summary=issue_summary,
        issue_description=issue_description,
        recorded_answers=recorded_answers,
        tenant_id=tenant_id,
        project_id=project_id,
        issue_key=issue_key,
    )
    decision_gate = precheck_policy.decision_gate
    gtd = precheck_policy.gtd

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


def _resolve_required_worker_capability(
    *,
    issue_summary: str | None,
    issue_description: str | None,
    issue_labels: list[str] | None,
    tenant_id: str | None = None,
    project_id: str | None = None,
    issue_key: str | None = None,
) -> tuple[str, tuple[str, ...], tuple[WorkerCapability, ...]]:
    label_parse: WorkerLabelParseResult = parse_worker_capability_labels(issue_labels)
    if label_parse.selected_capability is not None:
        return (
            label_parse.selected_capability.value,
            label_parse.invalid_labels,
            label_parse.conflicting_capabilities,
        )
    return infer_required_worker_capability(
        issue_summary=issue_summary,
        issue_description=issue_description,
        issue_labels=issue_labels,
        tenant_id=tenant_id,
        project_id=project_id,
        issue_key=issue_key,
    ), label_parse.invalid_labels, label_parse.conflicting_capabilities


def _invalid_worker_label_guard(invalid_labels: tuple[str, ...]) -> GoodToDoValidationResult | None:
    if not invalid_labels:
        return None
    invalid = ", ".join(invalid_labels)
    return GoodToDoValidationResult(
        valid=False,
        missing_criteria=("Worker capability labels must use canonical values.",),
        clarification_questions=(
            f"Replace invalid worker label(s): {invalid}. Use worker:linux or worker:macos.",
        ),
    )


def _conflicting_worker_capability_guard(
    conflicting_capabilities: tuple[WorkerCapability, ...],
) -> GoodToDoValidationResult | None:
    if not conflicting_capabilities:
        return None
    normalized = ", ".join(sorted(capability.value for capability in conflicting_capabilities))
    return GoodToDoValidationResult(
        valid=False,
        missing_criteria=("Worker capability labels must not conflict.",),
        clarification_questions=(
            f"Conflicting worker capability labels detected: {normalized}. Keep exactly one of worker:linux or worker:macos.",
        ),
    )
