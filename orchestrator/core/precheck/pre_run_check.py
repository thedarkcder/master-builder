from __future__ import annotations

from dataclasses import dataclass
from orchestrator.core.decision.gate import DecisionGateResult
from orchestrator.core.gtd import GoodToDoValidationResult
from orchestrator.core.precheck.policy import evaluate_precheck_policy
from orchestrator.core.worker.capability_normalization import WorkerCapability
from orchestrator.core.worker.capabilities import (
    infer_required_worker_capability,
    parse_worker_capability_labels,
    WorkerLabelParseResult,
    worker_label_for_capability,
)


def _precheck_outcome_enum(value: object):
    from orchestrator.core.decision.types import PrecheckOutcome

    return PrecheckOutcome.parse(value)


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
        from orchestrator.core.decision.types import PrecheckOutcome

        if not self.ready_label_missing:
            return self
        outcome = self.outcome
        if _precheck_outcome_enum(outcome) is PrecheckOutcome.MISSING_READY_LABEL:
            outcome = PrecheckOutcome.READY_FOR_AGENT.value
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


def resolve_precheck_outcome(
    *,
    decision_gate_triggered: bool,
    gtd_valid: bool,
    ready_label_present: bool,
    ready_label_required: bool,
) -> str:
    from orchestrator.core.decision.types import PrecheckOutcome

    if decision_gate_triggered:
        return PrecheckOutcome.DECISION_GATE_REQUIRED.value
    if not gtd_valid:
        return PrecheckOutcome.GTD_REQUIRED.value
    if ready_label_required and not ready_label_present:
        return PrecheckOutcome.MISSING_READY_LABEL.value
    return PrecheckOutcome.READY_FOR_AGENT.value


def evaluate_execution_readiness_only(
    *,
    tenant_id: str | None = None,
    project_id: str | None = None,
    project_policy_overrides: dict[str, object] | None = None,
    issue_key: str | None = None,
    issue_summary: str | None,
    issue_description: str | None,
    issue_labels: list[str] | None,
    ready_label: str | None,
) -> PreRunCheckResult:
    from orchestrator.core.decision.types import PrecheckOutcome

    normalized_labels = {str(label).strip().casefold() for label in issue_labels or []}
    normalized_ready_label = str(ready_label or "").strip() or None
    ready_label_present = bool(
        normalized_ready_label
        and normalized_ready_label.casefold() in normalized_labels
    )
    (
        required_worker_capability,
        invalid_worker_labels,
        conflicting_worker_capabilities,
    ) = _resolve_required_worker_capability(
        issue_summary=issue_summary,
        issue_description=issue_description,
        issue_labels=issue_labels,
        project_policy_overrides=project_policy_overrides,
        tenant_id=tenant_id,
        project_id=project_id,
        issue_key=issue_key,
    )
    required_worker_label = _required_worker_label(required_worker_capability)
    required_worker_label_present = (
        True
        if not required_worker_label
        else required_worker_label.casefold() in normalized_labels
    )
    invalid_label_guard = _invalid_worker_label_guard(invalid_worker_labels)
    conflicting_capability_guard = _conflicting_worker_capability_guard(
        conflicting_worker_capabilities
    )
    gtd_guard = invalid_label_guard or conflicting_capability_guard
    outcome = (
        PrecheckOutcome.EXECUTION_BLOCKED.value
        if gtd_guard is not None
        else resolve_precheck_outcome(
            decision_gate_triggered=False,
            gtd_valid=True,
            ready_label_present=ready_label_present,
            ready_label_required=normalized_ready_label is not None,
        )
    )
    return PreRunCheckResult(
        outcome=outcome,
        ready_label=normalized_ready_label,
        ready_label_present=ready_label_present,
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
                    else "Decision Gate permanently satisfied"
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
                    else "Proceed with execution."
                )
            ),
            tags=(),
        ),
        gtd=gtd_guard
        or GoodToDoValidationResult(
            valid=True,
            missing_criteria=(),
            clarification_questions=(),
        ),
    )


def evaluate_pre_run_check(
    *,
    tenant_id: str | None = None,
    project_id: str | None = None,
    project_policy_overrides: dict[str, object] | None = None,
    issue_key: str | None = None,
    issue_summary: str | None,
    issue_description: str | None,
    recorded_answers: list[dict[str, str]] | None = None,
    issue_labels: list[str] | None,
    ready_label: str | None,
) -> PreRunCheckResult:
    from orchestrator.core.decision.types import PrecheckOutcome

    normalized_labels = {str(label).strip().casefold() for label in issue_labels or []}
    normalized_ready_label = str(ready_label or "").strip() or None
    ready_label_present = bool(
        normalized_ready_label
        and normalized_ready_label.casefold() in normalized_labels
    )
    (
        required_worker_capability,
        invalid_worker_labels,
        conflicting_worker_capabilities,
    ) = _resolve_required_worker_capability(
        issue_summary=issue_summary,
        issue_description=issue_description,
        issue_labels=issue_labels,
        project_policy_overrides=project_policy_overrides,
        tenant_id=tenant_id,
        project_id=project_id,
        issue_key=issue_key,
    )
    required_worker_label = _required_worker_label(required_worker_capability)
    required_worker_label_present = (
        True
        if not required_worker_label
        else required_worker_label.casefold() in normalized_labels
    )
    invalid_label_guard = _invalid_worker_label_guard(invalid_worker_labels)
    conflicting_capability_guard = _conflicting_worker_capability_guard(
        conflicting_worker_capabilities
    )
    if invalid_label_guard is not None:
        return PreRunCheckResult(
            outcome=PrecheckOutcome.GTD_REQUIRED.value,
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
            outcome=PrecheckOutcome.GTD_REQUIRED.value,
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

    outcome = resolve_precheck_outcome(
        decision_gate_triggered=decision_gate.triggered,
        gtd_valid=gtd.valid,
        ready_label_present=ready_label_present,
        ready_label_required=normalized_ready_label is not None,
    )

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


def _required_worker_label(required_worker_capability: str) -> str:
    normalized = str(required_worker_capability or "").strip()
    if not normalized:
        return ""
    return worker_label_for_capability(normalized)


def _resolve_required_worker_capability(
    *,
    issue_summary: str | None,
    issue_description: str | None,
    issue_labels: list[str] | None,
    project_policy_overrides: dict[str, object] | None = None,
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
    return (
        infer_required_worker_capability(
            issue_summary=issue_summary,
            issue_description=issue_description,
            issue_labels=issue_labels,
            project_default_worker_capability=(project_policy_overrides or {}).get(
                "default_worker_capability"
            ),
            tenant_id=tenant_id,
            project_id=project_id,
            issue_key=issue_key,
        ),
        label_parse.invalid_labels,
        label_parse.conflicting_capabilities,
    )


def _invalid_worker_label_guard(
    invalid_labels: tuple[str, ...],
) -> GoodToDoValidationResult | None:
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
    normalized = ", ".join(
        sorted(capability.value for capability in conflicting_capabilities)
    )
    return GoodToDoValidationResult(
        valid=False,
        missing_criteria=("Worker capability labels must not conflict.",),
        clarification_questions=(
            f"Conflicting worker capability labels detected: {normalized}. Keep exactly one of worker:linux or worker:macos.",
        ),
    )
