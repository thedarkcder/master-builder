from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import hashlib

from orchestrator.core.decision.gate import DecisionGateResult
from orchestrator.core.decision.planner import DecisionPlannerResult
from orchestrator.core.decision.types import (
    DecisionClassification,
    DecisionLabelAction,
    DecisionQuestionKind,
    DecisionSource,
    ExecutionGateReason,
    ExecutionGateResolution,
    ExecutionGateState,
    IngressDecision,
    PrecheckOutcome,
    ReadinessDecision,
    ReadinessState,
    WorkerDecision,
)
from orchestrator.core.gtd import GoodToDoValidationResult
from orchestrator.core.precheck.pre_run_check import PreRunCheckResult
from orchestrator.core.precheck.slot_registry import canonical_slot_id
from orchestrator.core.runtime.payload_models import PlannerGateStatus
from orchestrator.storage.models import DecisionCase


class DecisionEvent(str, Enum):
    EVALUATE_INGRESS = "evaluate_ingress"


class DecisionStateTransition(str, Enum):
    OPEN_CYCLE_BLOCKED = "open_cycle_blocked"
    OPEN_CYCLE_CLEAR_AND_CLOSE = "open_cycle_clear_and_close"
    TERMINAL_GATE_CLOSED_CLEAR = "terminal_gate_closed_clear"
    REUSE_CLEAR_FINGERPRINT = "reuse_clear_fingerprint"
    EVALUATE_FRESH = "evaluate_fresh"


@dataclass(frozen=True)
class DecisionState:
    has_case: bool
    has_open_cycle: bool
    unresolved_question_count: int
    decision_gate_closed_permanently: bool
    case_classification: DecisionClassification
    case_issue_fingerprint: str
    current_issue_fingerprint: str


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


@dataclass(frozen=True)
class PrecheckSnapshot:
    outcome: PrecheckOutcome
    ready_label: str | None
    ready_label_present: bool
    required_worker_capability: str
    required_worker_label: str
    required_worker_label_present: bool
    decision_gate: DecisionGateResult
    gtd: GoodToDoValidationResult

    @classmethod
    def from_precheck(cls, pre_check: PreRunCheckResult) -> "PrecheckSnapshot":
        return cls(
            outcome=PrecheckOutcome.parse(pre_check.outcome) or PrecheckOutcome.POLICY_EVAL_FAILED,
            ready_label=pre_check.ready_label,
            ready_label_present=pre_check.ready_label_present,
            required_worker_capability=pre_check.required_worker_capability,
            required_worker_label=pre_check.required_worker_label,
            required_worker_label_present=pre_check.required_worker_label_present,
            decision_gate=pre_check.decision_gate,
            gtd=pre_check.gtd,
        )

    def to_precheck(self) -> PreRunCheckResult:
        return PreRunCheckResult(
            outcome=self.outcome.value,
            ready_label=self.ready_label,
            ready_label_present=self.ready_label_present,
            required_worker_capability=self.required_worker_capability,
            required_worker_label=self.required_worker_label,
            required_worker_label_present=self.required_worker_label_present,
            decision_gate=self.decision_gate,
            gtd=self.gtd,
        )

    @classmethod
    def load(cls, payload: object) -> "PrecheckSnapshot" | None:
        if not isinstance(payload, dict):
            return None
        decision_gate_payload = payload.get("decision_gate")
        gtd_payload = payload.get("gtd")
        if not isinstance(decision_gate_payload, dict) or not isinstance(gtd_payload, dict):
            return None
        outcome = PrecheckOutcome.parse(payload.get("outcome"))
        if outcome is None:
            return None
        return cls(
            outcome=outcome,
            ready_label=str(payload.get("ready_label") or "").strip() or None,
            ready_label_present=bool(payload.get("ready_label_present", False)),
            required_worker_capability=str(payload.get("required_worker_capability") or "").strip(),
            required_worker_label=str(payload.get("required_worker_label") or "").strip(),
            required_worker_label_present=bool(payload.get("required_worker_label_present", False)),
            decision_gate=DecisionGateResult(
                triggered=bool(decision_gate_payload.get("triggered")),
                reason=str(decision_gate_payload.get("reason") or "").strip(),
                missing_sections=_string_tuple(decision_gate_payload.get("missing_sections")),
                questions=_string_tuple(decision_gate_payload.get("questions")),
                recommendation=str(decision_gate_payload.get("recommendation") or "").strip(),
                tags=_string_tuple(decision_gate_payload.get("tags")),
            ),
            gtd=GoodToDoValidationResult(
                valid=bool(gtd_payload.get("valid")),
                missing_criteria=_string_tuple(gtd_payload.get("missing_criteria")),
                clarification_questions=_string_tuple(gtd_payload.get("clarification_questions")),
            ),
        )

    def dump(self) -> dict[str, object]:
        return {
            "outcome": self.outcome.value,
            "ready_label": self.ready_label,
            "ready_label_present": self.ready_label_present,
            "required_worker_capability": self.required_worker_capability,
            "required_worker_label": self.required_worker_label,
            "required_worker_label_present": self.required_worker_label_present,
            "decision_gate": {
                "triggered": self.decision_gate.triggered,
                "reason": self.decision_gate.reason,
                "missing_sections": list(self.decision_gate.missing_sections),
                "questions": list(self.decision_gate.questions),
                "recommendation": self.decision_gate.recommendation,
                "tags": list(self.decision_gate.tags),
            },
            "gtd": {
                "valid": self.gtd.valid,
                "missing_criteria": list(self.gtd.missing_criteria),
                "clarification_questions": list(self.gtd.clarification_questions),
            },
        }


@dataclass(frozen=True)
class DecisionResultSnapshot:
    classification: DecisionClassification
    issue_labels: tuple[str, ...]
    missing_slots: tuple[str, ...]
    auto_resolved_slots: tuple[str, ...]
    block_reason: str | None
    guidance: str | None
    policy_error: str | None
    pre_check: PrecheckSnapshot | None

    @classmethod
    def from_decision(
        cls,
        *,
        decision: IngressDecision,
        classification: str,
        issue_labels: list[str],
        missing_slots: list[str],
        auto_resolved_slots: list[str],
    ) -> "DecisionResultSnapshot":
        return cls(
            classification=DecisionClassification.parse(classification),
            issue_labels=tuple(str(label).strip() for label in issue_labels if str(label).strip()),
            missing_slots=tuple(str(slot).strip() for slot in missing_slots if str(slot).strip()),
            auto_resolved_slots=tuple(
                str(slot).strip() for slot in auto_resolved_slots if str(slot).strip()
            ),
            block_reason=decision.block_reason,
            guidance=decision.guidance,
            policy_error=decision.policy_error,
            pre_check=(
                PrecheckSnapshot.from_precheck(decision.pre_check)
                if isinstance(decision.pre_check, PreRunCheckResult)
                else None
            ),
        )

    @classmethod
    def load(cls, payload: object) -> "DecisionResultSnapshot" | None:
        if not isinstance(payload, dict):
            return None
        return cls(
            classification=DecisionClassification.parse(payload.get("classification")),
            issue_labels=tuple(
                str(label).strip() for label in payload.get("issue_labels", []) if str(label).strip()
            ),
            missing_slots=tuple(
                str(slot).strip() for slot in payload.get("missing_slots", []) if str(slot).strip()
            ),
            auto_resolved_slots=tuple(
                str(slot).strip()
                for slot in payload.get("auto_resolved_slots", [])
                if str(slot).strip()
            ),
            block_reason=str(payload.get("block_reason") or "").strip() or None,
            guidance=str(payload.get("guidance") or "").strip() or None,
            policy_error=str(payload.get("policy_error") or "").strip() or None,
            pre_check=PrecheckSnapshot.load(payload.get("pre_check")),
        )

    def dump(self) -> dict[str, object]:
        return {
            "classification": self.classification.value,
            "issue_labels": list(self.issue_labels),
            "missing_slots": list(self.missing_slots),
            "auto_resolved_slots": list(self.auto_resolved_slots),
            "block_reason": self.block_reason,
            "guidance": self.guidance,
            "policy_error": self.policy_error,
            "pre_check": self.pre_check.dump() if self.pre_check is not None else None,
        }


def stable_short_hash(value: str) -> str:
    return hashlib.sha1(str(value).encode("utf-8")).hexdigest()[:10]


def decision_classification_for_precheck(pre_check: object) -> DecisionClassification:
    decision_gate = bool(getattr(pre_check, "decision_gate_triggered", False))
    gtd_valid_raw = getattr(pre_check, "gtd_valid", None)
    if isinstance(gtd_valid_raw, bool):
        gtd_missing = not gtd_valid_raw
    else:
        gtd_missing = bool(getattr(pre_check, "gtd_missing_criteria", ()) or getattr(pre_check, "gtd_clarification_questions", ()))
    if decision_gate and gtd_missing:
        return DecisionClassification.BOTH
    if decision_gate:
        return DecisionClassification.DECISION_GATE
    if gtd_missing:
        return DecisionClassification.GTD
    return DecisionClassification.CLEAR


def _canonical_slot_name(raw_value: object) -> str:
    return canonical_slot_id(raw_value)


def decision_missing_slots_for_precheck(pre_check: object) -> list[str]:
    slots: list[str] = []
    decision_gate = getattr(pre_check, "decision_gate", None)
    if decision_gate is not None:
        missing_sections = getattr(decision_gate, "missing_sections", ())
        if isinstance(missing_sections, (list, tuple)):
            for item in missing_sections:
                canonical = _canonical_slot_name(item)
                if canonical and canonical not in slots:
                    slots.append(canonical)
    gtd_missing = getattr(pre_check, "gtd_missing_criteria", ())
    if isinstance(gtd_missing, (list, tuple)):
        for item in gtd_missing:
            canonical = _canonical_slot_name(item)
            if canonical and canonical not in slots:
                slots.append(canonical)
    return slots


@dataclass(frozen=True)
class ReducedPlannerDecision:
    decision: IngressDecision
    classification: DecisionClassification
    question_set: list[dict[str, object]]
    question_states: list[dict[str, object]]


def planner_classification_for_gate_status(*, gate_status: PlannerGateStatus) -> DecisionClassification:
    if gate_status is PlannerGateStatus.BLOCKED_DECISION_GATE:
        return DecisionClassification.DECISION_GATE
    if gate_status is PlannerGateStatus.BLOCKED_GTD:
        return DecisionClassification.GTD
    if gate_status is PlannerGateStatus.BLOCKED_BOTH:
        return DecisionClassification.BOTH
    return DecisionClassification.CLEAR


def clear_decision_planner_result() -> DecisionPlannerResult:
    return DecisionPlannerResult(
        gate_status=PlannerGateStatus.CLEAR,
        reason="",
        questions=(),
        question_states=(),
        resolved_items=(),
        missing_items=(),
        captured_answer_summary=None,
    )


def build_question_set(*, pre_check: object, classification: str) -> list[dict]:
    if pre_check is None:
        return []
    items: list[dict[str, str]] = []
    parsed_classification = DecisionClassification.parse(classification)
    if parsed_classification.includes_decision_gate:
        decision_gate = getattr(pre_check, "decision_gate", None)
        questions = getattr(decision_gate, "questions", ()) if decision_gate is not None else ()
        for question in questions:
            text = str(question or "").strip()
            if not text:
                continue
            items.append(
                {
                    "id": f"dg_{stable_short_hash(text)}",
                    "kind": DecisionQuestionKind.DECISION_GATE.value,
                    "text": text,
                }
            )
    if parsed_classification.includes_gtd:
        for question in getattr(pre_check, "gtd_clarification_questions", ()):
            text = str(question or "").strip()
            if not text:
                continue
            items.append(
                {
                    "id": f"gtd_{stable_short_hash(text)}",
                    "kind": DecisionQuestionKind.GTD.value,
                    "text": text,
                }
            )
    deduped: list[dict] = []
    seen: set[str] = set()
    for item in items:
        item_id = str(item.get("id") or "").strip()
        if not item_id or item_id in seen:
            continue
        deduped.append(item)
        seen.add(item_id)
    return deduped


def reduce_decision_planner_result(
    *,
    decision: IngressDecision,
    planner_result: DecisionPlannerResult,
) -> ReducedPlannerDecision:
    reduced_decision = _decision_with_planner_result(decision=decision, planner_result=planner_result)
    return ReducedPlannerDecision(
        decision=reduced_decision,
        classification=planner_classification_for_gate_status(gate_status=planner_result.gate_status),
        question_set=_planner_question_set(planner_result),
        question_states=_planner_question_state_payload(planner_result),
    )


def coerce_clear_decision_from_case(*, decision: IngressDecision, case: DecisionCase) -> IngressDecision:
    normalized_pre_check = (
        decision.pre_check
        if isinstance(decision.pre_check, PreRunCheckResult)
        else _synthetic_clear_pre_check(case=case)
    )
    reduced = reduce_decision_planner_result(
        decision=IngressDecision(
            source=decision.source,
            pre_check=normalized_pre_check,
            block_reason=blocking_reason_for_precheck(normalized_pre_check),
            guidance=None,
            policy_error=None,
            label_actions=decision.label_actions,
        ),
        planner_result=clear_decision_planner_result(),
    )
    return reduced.decision


def apply_frozen_cycle_questions(
    *,
    pre_check: object,
    cycle_question_set: list[dict[str, object]],
    unresolved_question_ids: list[str],
    cycle_reason: str | None,
    classification: DecisionClassification | object,
) -> object:
    parsed_classification = DecisionClassification.parse(classification)
    unresolved_ids = {
        str(question_id).strip()
        for question_id in unresolved_question_ids
        if str(question_id).strip()
    }
    decision_gate_questions = [
        str(item.get("text") or "").strip()
        for item in cycle_question_set
        if (
            DecisionQuestionKind.parse(item.get("kind")) is DecisionQuestionKind.DECISION_GATE
            and str(item.get("text") or "").strip()
            and (
                not str(item.get("id") or "").strip()
                or str(item.get("id") or "").strip() in unresolved_ids
            )
        )
    ]
    gtd_questions = [
        str(item.get("text") or "").strip()
        for item in cycle_question_set
        if (
            DecisionQuestionKind.parse(item.get("kind")) is DecisionQuestionKind.GTD
            and str(item.get("text") or "").strip()
            and (
                not str(item.get("id") or "").strip()
                or str(item.get("id") or "").strip() in unresolved_ids
            )
        )
    ]
    resolved = pre_check
    decision_gate = getattr(resolved, "decision_gate", None)
    gtd = getattr(resolved, "gtd", None)
    if parsed_classification.includes_decision_gate and decision_gate is not None:
        next_decision_gate = decision_gate
        if cycle_reason:
            next_decision_gate = replace(next_decision_gate, reason=cycle_reason)
        next_decision_gate = replace(next_decision_gate, questions=tuple(decision_gate_questions))
        resolved = replace(resolved, decision_gate=next_decision_gate)
    if parsed_classification.includes_gtd and gtd is not None:
        resolved = replace(resolved, gtd=replace(gtd, clarification_questions=tuple(gtd_questions)))
    return resolved


@dataclass(frozen=True)
class ExecutionAdmissionDecision:
    can_enqueue: bool
    precheck_outcome: str | None
    required_worker_capability: str | None = None
    reason: ExecutionAdmissionReason | None = None
    guidance: str | None = None
    detail: str | None = None
    ready_label: str | None = None

    @property
    def blocked(self) -> bool:
        return not self.can_enqueue

    @property
    def reason_code(self) -> str | None:
        return self.reason.value if self.reason is not None else None


ExecutionAdmissionOutcome = ExecutionAdmissionDecision


class ExecutionAdmissionReason(str, Enum):
    DECISION_GATE_REQUIRED = "decision_gate_required"
    GTD_REQUIRED = "gtd_required"
    MISSING_READY_LABEL = "missing_ready_label"
    EXECUTION_BLOCKED = "execution_blocked"
    POLICY_EVAL_FAILED = "policy_eval_failed"
    NO_RETRYABLE_RUN = "no_retryable_run"
    RUN_ALREADY_ACTIVE = "run_already_active"
    DUPLICATE_RUN = "duplicate_run"
    DUPLICATE_DELIVERY = "duplicate_delivery"
    TENANT_CONCURRENCY_LIMIT_REACHED = "tenant_concurrency_limit_reached"
    PR_REMEDIATION_ATTEMPT_LIMIT_REACHED = "pr_remediation_attempt_limit_reached"
    PROJECT_NOT_MAPPED = "project_not_mapped"
    READY_FOR_AGENT_BACKLOG = "ready_for_agent_backlog"
    ISSUE_IN_BACKLOG = "issue_in_backlog"
    ISSUE_NOT_ON_BOARD = "issue_not_on_board"
    BOARD_GATE_CHECK_FAILED = "board_gate_check_failed"
    BOARD_GATE_UNCONFIGURED = "board_gate_unconfigured"

    @property
    def guidance(self) -> str:
        if self is ExecutionAdmissionReason.DECISION_GATE_REQUIRED:
            return "Decision Gate is required before execution. Reply with the missing clarifications."
        if self is ExecutionAdmissionReason.GTD_REQUIRED:
            return "Good To Do details are incomplete. Add the missing GTD details, then rerun."
        if self is ExecutionAdmissionReason.MISSING_READY_LABEL:
            return "Issue is missing the configured ready label."
        if self is ExecutionAdmissionReason.EXECUTION_BLOCKED:
            return "Execution readiness checks failed. Resolve sync/capability blockers, then rerun."
        if self is ExecutionAdmissionReason.POLICY_EVAL_FAILED:
            return "Pre-run policy evaluation failed. Resolve policy/runtime errors before rerunning."
        if self is ExecutionAdmissionReason.NO_RETRYABLE_RUN:
            return "No failed/blocked/cancelled run is available to retry for this issue."
        if self is ExecutionAdmissionReason.RUN_ALREADY_ACTIVE:
            return "A run for this issue is already active."
        if self is ExecutionAdmissionReason.DUPLICATE_DELIVERY:
            return "This webhook delivery was already processed."
        if self is ExecutionAdmissionReason.TENANT_CONCURRENCY_LIMIT_REACHED:
            return "The tenant concurrency limit is reached; wait for an active run to finish."
        if self is ExecutionAdmissionReason.PR_REMEDIATION_ATTEMPT_LIMIT_REACHED:
            return "Automatic PR remediation attempt limit reached for this commit head."
        if self is ExecutionAdmissionReason.PROJECT_NOT_MAPPED:
            return "Issue key is not mapped to an active project."
        if self is ExecutionAdmissionReason.READY_FOR_AGENT_BACKLOG:
            return "Issue is ready-for-agent in backlog; move it to To Do to start execution."
        if self is ExecutionAdmissionReason.ISSUE_IN_BACKLOG:
            return "Issue is currently in backlog for the configured board; move it onto the board before running."
        if self is ExecutionAdmissionReason.ISSUE_NOT_ON_BOARD:
            return "Issue is not present on the configured board; place it on the board before running."
        if self is ExecutionAdmissionReason.BOARD_GATE_CHECK_FAILED:
            return "Board-location gate check failed; verify Atlassian connection/scopes and board configuration."
        if self is ExecutionAdmissionReason.BOARD_GATE_UNCONFIGURED:
            return "Run board gating is enabled but no valid board id is configured."
        if self is ExecutionAdmissionReason.DUPLICATE_RUN:
            return "Run was not queued due to current execution policy."
        return "Run was not queued due to current execution policy."


@dataclass(frozen=True)
class RunGateBlock:
    reason: str
    guidance: str
    detail: str | None
    ready_label: str | None


@dataclass(frozen=True)
class WorkerBlockedOutcome:
    reason: str
    next_steps: tuple[str, ...]
    ready_label: str | None
    block_reason: str | None
    pre_check_outcome: str | None

    @classmethod
    def load(cls, payload: object) -> WorkerBlockedOutcome | None:
        if not isinstance(payload, dict):
            return None
        next_steps_raw = payload.get("next_steps")
        next_steps = (
            tuple(str(item).strip() for item in next_steps_raw if str(item).strip())
            if isinstance(next_steps_raw, (list, tuple))
            else ()
        )
        reason = str(payload.get("reason") or "").strip()
        if not reason:
            return None
        ready_label = str(payload.get("ready_label") or "").strip() or None
        block_reason = str(payload.get("block_reason") or "").strip() or None
        pre_check_outcome = str(payload.get("pre_check_outcome") or "").strip() or None
        return cls(
            reason=reason,
            next_steps=next_steps,
            ready_label=ready_label,
            block_reason=block_reason,
            pre_check_outcome=pre_check_outcome,
        )

    def dump(self) -> dict[str, object]:
        return {
            "reason": self.reason,
            "next_steps": list(self.next_steps),
            "ready_label": self.ready_label,
            "block_reason": self.block_reason,
            "pre_check_outcome": self.pre_check_outcome,
        }


def blocking_reason_for_precheck(pre_check: object) -> str | None:
    return blocking_reason_for_outcome(getattr(pre_check, "outcome", None))


def blocking_reason_for_outcome(outcome: object) -> str | None:
    parsed = PrecheckOutcome.parse(outcome)
    if parsed is None or (not parsed.is_decision_block and parsed is not PrecheckOutcome.MISSING_READY_LABEL):
        return None
    return parsed.value


def resolve_decision_state_transition(
    *,
    state: DecisionState,
    event: DecisionEvent = DecisionEvent.EVALUATE_INGRESS,
) -> DecisionStateTransition:
    _ = event
    if state.has_case and state.has_open_cycle:
        if state.unresolved_question_count > 0:
            return DecisionStateTransition.OPEN_CYCLE_BLOCKED
        return DecisionStateTransition.OPEN_CYCLE_CLEAR_AND_CLOSE

    if state.has_case and state.decision_gate_closed_permanently:
        return DecisionStateTransition.TERMINAL_GATE_CLOSED_CLEAR

    if (
        state.has_case
        and not state.has_open_cycle
        and state.case_classification is DecisionClassification.CLEAR
        and state.case_issue_fingerprint == state.current_issue_fingerprint
    ):
        return DecisionStateTransition.REUSE_CLEAR_FINGERPRINT

    return DecisionStateTransition.EVALUATE_FRESH


def execution_gate_reason_for_precheck_outcome(
    *,
    outcome: PrecheckOutcome,
    detail: str | None = None,
    ready_label: str | None = None,
) -> ExecutionGateReason:
    reason_code = outcome.value
    normalized_detail = str(detail or "").strip() or None
    normalized_ready_label = str(ready_label or "").strip() or None
    parsed_reason = parse_execution_admission_reason(reason_code)
    guidance = (
        parsed_reason.guidance
        if parsed_reason is not None
        else "Run was not queued due to current execution policy."
    )
    if outcome is PrecheckOutcome.MISSING_READY_LABEL and normalized_ready_label:
        guidance = f"{guidance} ({normalized_ready_label})"
    return ExecutionGateReason(
        reason_code=reason_code,
        guidance=guidance,
        detail=normalized_detail,
        ready_label=normalized_ready_label,
    )


def guidance_for_precheck_block_reason(
    *,
    block_reason: str | None,
    ready_label: str | None = None,
) -> str | None:
    parsed = PrecheckOutcome.parse(block_reason)
    if parsed is None:
        return None
    return execution_gate_reason_for_precheck_outcome(
        outcome=parsed,
        ready_label=ready_label,
    ).guidance


def ingress_decision_from_precheck(
    *,
    source: DecisionSource,
    pre_check: PreRunCheckResult,
    policy_error: str | None = None,
    label_actions: tuple[DecisionLabelAction, ...] = (),
) -> IngressDecision:
    block_reason = blocking_reason_for_precheck(pre_check)
    return IngressDecision(
        source=source,
        pre_check=pre_check,
        block_reason=block_reason,
        guidance=guidance_for_precheck_block_reason(block_reason=block_reason),
        policy_error=policy_error,
        label_actions=label_actions,
    )


def ingress_policy_error_decision(
    *,
    source: DecisionSource,
    policy_error: str,
) -> IngressDecision:
    return IngressDecision(
        source=source,
        pre_check=None,
        block_reason=PrecheckOutcome.POLICY_EVAL_FAILED.value,
        guidance=guidance_for_precheck_block_reason(
            block_reason=PrecheckOutcome.POLICY_EVAL_FAILED.value,
        ),
        policy_error=policy_error,
        label_actions=(),
    )


def resolve_readiness_decision(
    *,
    policy_error: str | None,
    block_reason: str | None,
    classification: object,
) -> ReadinessDecision:
    if str(policy_error or "").strip():
        return ReadinessDecision(
            state=ReadinessState.POLICY_ERROR,
            reason_code=PrecheckOutcome.POLICY_EVAL_FAILED,
        )

    parsed_block_reason = PrecheckOutcome.parse(block_reason)
    if parsed_block_reason is PrecheckOutcome.MISSING_READY_LABEL:
        return ReadinessDecision(
            state=ReadinessState.BLOCKED_READY_LABEL,
            reason_code=PrecheckOutcome.MISSING_READY_LABEL,
        )
    if parsed_block_reason is not None and parsed_block_reason.is_decision_block:
        return ReadinessDecision(
            state=ReadinessState.BLOCKED_DECISION,
            reason_code=parsed_block_reason,
        )

    normalized_classification = DecisionClassification.parse(classification)
    if normalized_classification.blocks_execution:
        return ReadinessDecision(
            state=ReadinessState.BLOCKED_DECISION,
            reason_code=PrecheckOutcome.DECISION_GATE_REQUIRED,
        )
    return ReadinessDecision(state=ReadinessState.READY)


def case_state_for_decision(*, decision: IngressDecision) -> str:
    pre_check = decision.pre_check
    parsed_block_reason = PrecheckOutcome.parse(decision.block_reason)
    if parsed_block_reason is PrecheckOutcome.DECISION_GATE_REQUIRED:
        return "blocked_decision_gate"
    if parsed_block_reason in {PrecheckOutcome.GTD_REQUIRED, PrecheckOutcome.EXECUTION_BLOCKED}:
        return "blocked_gtd"
    if pre_check is not None and PrecheckOutcome.parse(getattr(pre_check, "outcome", None)) is PrecheckOutcome.READY_FOR_AGENT:
        return "ready_for_execution"
    return "clear"


def is_question_driven_state(*, classification: DecisionClassification | str, block_reason: str | None) -> bool:
    parsed_classification = (
        classification
        if isinstance(classification, DecisionClassification)
        else DecisionClassification.parse(classification)
    )
    parsed_block_reason = PrecheckOutcome.parse(block_reason)
    return parsed_classification.blocks_execution and parsed_block_reason in {
        PrecheckOutcome.DECISION_GATE_REQUIRED,
        PrecheckOutcome.GTD_REQUIRED,
    }


def decision_reason(*, pre_check: object, classification: str) -> str | None:
    if pre_check is None:
        return None
    parsed_classification = DecisionClassification.parse(classification)
    if parsed_classification.includes_decision_gate:
        decision_gate = getattr(pre_check, "decision_gate", None)
        reason = str(getattr(decision_gate, "reason", "") or "").strip() if decision_gate is not None else ""
        if reason:
            return reason
    if parsed_classification.includes_gtd:
        missing = [
            str(item).strip()
            for item in getattr(pre_check, "gtd_missing_criteria", ())
            if str(item).strip()
        ]
        if missing:
            return "Missing GTD criteria: " + ", ".join(missing)
    return None


def _planner_question_set(planner_result: DecisionPlannerResult) -> list[dict[str, object]]:
    open_question_overrides = {
        item.question_id: item
        for item in planner_result.questions
        if str(item.question_id or "").strip()
    }
    states = planner_result.question_states or planner_result.questions
    question_set: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in states:
        if item.question_id in seen:
            continue
        seen.add(item.question_id)
        override = open_question_overrides.get(item.question_id)
        question_set.append(
            {
                "id": item.question_id,
                "kind": override.kind if override is not None else item.kind,
                "text": override.question if override is not None else item.question,
                "status": item.status,
                "detail": override.detail if override is not None else item.detail,
            }
        )
    return question_set


def _planner_question_state_payload(planner_result: DecisionPlannerResult) -> list[dict[str, object]]:
    open_question_overrides = {
        item.question_id: item
        for item in planner_result.questions
        if str(item.question_id or "").strip()
    }
    return [
        {
            "question_id": item.question_id,
            "kind": (
                open_question_overrides[item.question_id].kind
                if item.question_id in open_question_overrides
                else item.kind
            ),
            "question": (
                open_question_overrides[item.question_id].question
                if item.question_id in open_question_overrides
                else item.question
            ),
            "status": item.status,
            "detail": (
                open_question_overrides[item.question_id].detail
                if item.question_id in open_question_overrides
                else item.detail
            ),
        }
        for item in (planner_result.question_states or planner_result.questions)
    ]


def _planner_block_reason(classification: DecisionClassification) -> str | None:
    if classification.includes_decision_gate:
        return PrecheckOutcome.DECISION_GATE_REQUIRED.value
    if classification is DecisionClassification.GTD:
        return PrecheckOutcome.GTD_REQUIRED.value
    return None


def _decision_with_planner_result(
    *,
    decision: IngressDecision,
    planner_result: DecisionPlannerResult,
) -> IngressDecision:
    pre_check = decision.pre_check
    if not isinstance(pre_check, PreRunCheckResult):
        return decision
    classification = planner_classification_for_gate_status(gate_status=planner_result.gate_status)
    reason = planner_result.reason
    decision_gate_questions = tuple(
        item.question
        for item in planner_result.questions
        if item.kind == "decision_gate"
    )
    gtd_questions = tuple(
        item.question
        for item in planner_result.questions
        if item.kind == "gtd"
    )
    missing_items = tuple(planner_result.missing_items)
    if classification is DecisionClassification.CLEAR:
        outcome = (
            PrecheckOutcome.MISSING_READY_LABEL.value
            if pre_check.ready_label_missing
            else PrecheckOutcome.READY_FOR_AGENT.value
        )
        updated_pre_check = replace(
            pre_check,
            outcome=outcome,
            decision_gate=DecisionGateResult(
                triggered=False,
                reason="Decision Gate not required",
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
        return ingress_decision_from_precheck(
            source=decision.source,
            pre_check=updated_pre_check,
            policy_error=None,
            label_actions=decision.label_actions,
        )

    updated_pre_check = replace(
        pre_check,
        outcome=(
            PrecheckOutcome.DECISION_GATE_REQUIRED.value
            if classification.includes_decision_gate
            else PrecheckOutcome.GTD_REQUIRED.value
        ),
        decision_gate=DecisionGateResult(
            triggered=classification.includes_decision_gate,
            reason=reason if classification.includes_decision_gate else "Decision Gate not required",
            missing_sections=missing_items if classification.includes_decision_gate else (),
            questions=decision_gate_questions,
            recommendation=(
                "Clarification required before execution."
                if classification.includes_decision_gate
                else pre_check.decision_gate.recommendation
            ),
            tags=pre_check.decision_gate.tags,
        ),
        gtd=GoodToDoValidationResult(
            valid=not classification.includes_gtd,
            missing_criteria=missing_items if classification.includes_gtd else (),
            clarification_questions=gtd_questions,
        ),
    )
    return ingress_decision_from_precheck(
        source=decision.source,
        pre_check=updated_pre_check,
        policy_error=decision.policy_error,
        label_actions=decision.label_actions,
    )


def decision_from_snapshot(
    *,
    snapshot: dict[str, object],
    source: str,
    classification: str,
    cycle: object | None,
    case: object,
) -> IngressDecision:
    pre_check_snapshot = PrecheckSnapshot.load(snapshot.get("pre_check"))
    pre_check = pre_check_snapshot.to_precheck() if pre_check_snapshot is not None else None
    if pre_check is not None and cycle is not None and getattr(cycle, "status", None) == "open":
        pre_check = apply_frozen_cycle_questions(
            pre_check=pre_check,
            cycle_question_set=list(getattr(cycle, "question_set_json", ())),
            unresolved_question_ids=list(getattr(cycle, "unresolved_question_ids_json", ())),
            cycle_reason=getattr(cycle, "reason", None),
            classification=DecisionClassification.parse(classification),
        )

    if DecisionClassification.parse(classification) is DecisionClassification.CLEAR:
        normalized_pre_check = pre_check
        if normalized_pre_check is None:
            normalized_pre_check = _synthetic_clear_pre_check(case=case)
        if PrecheckOutcome.parse(getattr(normalized_pre_check, "outcome", None)) is PrecheckOutcome.DECISION_GATE_REQUIRED:
            normalized_pre_check = PreRunCheckResult(
                outcome=PrecheckOutcome.READY_FOR_AGENT.value,
                ready_label=normalized_pre_check.ready_label,
                ready_label_present=normalized_pre_check.ready_label_present,
                required_worker_capability=normalized_pre_check.required_worker_capability,
                required_worker_label=normalized_pre_check.required_worker_label,
                required_worker_label_present=normalized_pre_check.required_worker_label_present,
                decision_gate=normalized_pre_check.decision_gate,
                gtd=normalized_pre_check.gtd,
            )
        return ingress_decision_from_precheck(
            source=source,  # type: ignore[arg-type]
            pre_check=normalized_pre_check,
            policy_error=None,
            label_actions=(),
        )

    return IngressDecision(
        source=source,  # type: ignore[arg-type]
        pre_check=pre_check,
        block_reason=str(snapshot.get("block_reason") or getattr(case, "blocked_reason", None) or "").strip() or None,
        guidance=str(snapshot.get("guidance") or "").strip() or None,
        policy_error=str(snapshot.get("policy_error") or "").strip() or None,
        label_actions=(),
    )


def _synthetic_clear_pre_check(*, case: DecisionCase) -> PreRunCheckResult:
    ready_label = str(case.ready_label or "").strip() or None
    ready_label_present = bool(case.ready_label_present)
    outcome = (
        PrecheckOutcome.MISSING_READY_LABEL.value
        if ready_label and not ready_label_present
        else PrecheckOutcome.READY_FOR_AGENT.value
    )
    return PreRunCheckResult(
        outcome=outcome,
        ready_label=ready_label,
        ready_label_present=ready_label_present,
        required_worker_capability=str(case.required_worker_capability or "").strip(),
        required_worker_label=str(case.required_worker_label or "").strip(),
        required_worker_label_present=bool(case.required_worker_label_present),
        decision_gate=DecisionGateResult(
            triggered=False,
            reason="Decision Gate not required",
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


def resolve_execution_gate_state(
    *,
    decision: IngressDecision,
    classification: object,
) -> ExecutionGateResolution:
    readiness = resolve_readiness_decision(
        policy_error=decision.policy_error,
        block_reason=decision.block_reason,
        classification=classification,
    )
    if decision.pre_check is None or readiness.state == ReadinessState.POLICY_ERROR:
        return ExecutionGateResolution(
            state=ExecutionGateState.POLICY_ERROR,
            reason=execution_gate_reason_for_precheck_outcome(
                outcome=PrecheckOutcome.POLICY_EVAL_FAILED,
                detail=str(decision.policy_error or "").strip() or None,
            ),
        )

    if readiness.state == ReadinessState.BLOCKED_READY_LABEL:
        ready_label = str(getattr(decision.pre_check, "ready_label", "") or "").strip() or None
        return ExecutionGateResolution(
            state=ExecutionGateState.BLOCK_READY_LABEL,
            reason=execution_gate_reason_for_precheck_outcome(
                outcome=PrecheckOutcome.MISSING_READY_LABEL,
                ready_label=ready_label,
            ),
        )

    if readiness.state == ReadinessState.BLOCKED_DECISION:
        detail = str(getattr(decision.pre_check, "decision_gate_reason", "") or "").strip() or None
        return ExecutionGateResolution(
            state=ExecutionGateState.BLOCK_DECISION,
            reason=execution_gate_reason_for_precheck_outcome(
                outcome=readiness.reason_code or PrecheckOutcome.DECISION_GATE_REQUIRED,
                detail=detail,
            ),
        )

    return ExecutionGateResolution(state=ExecutionGateState.ALLOW_EXECUTION)


def _parse_admission_reason(raw_value: str | None) -> ExecutionAdmissionReason | None:
    normalized = str(raw_value or "").strip()
    if not normalized:
        return None
    try:
        return ExecutionAdmissionReason(normalized)
    except ValueError:
        return None


def parse_execution_admission_reason(raw_value: object) -> ExecutionAdmissionReason | None:
    return _parse_admission_reason(str(getattr(raw_value, "value", raw_value) or ""))


def build_execution_admission_block(
    *,
    reason: ExecutionAdmissionReason,
    detail: str | None = None,
    ready_label: str | None = None,
    precheck_outcome: str | None = None,
) -> ExecutionAdmissionDecision:
    normalized_detail = str(detail or "").strip() or None
    normalized_ready_label = str(ready_label or "").strip() or None
    guidance = reason.guidance
    if reason is ExecutionAdmissionReason.MISSING_READY_LABEL and normalized_ready_label:
        guidance = f"{guidance} ({normalized_ready_label})"
    return ExecutionAdmissionDecision(
        can_enqueue=False,
        precheck_outcome=precheck_outcome,
        required_worker_capability=None,
        reason=reason,
        guidance=guidance,
        detail=normalized_detail,
        ready_label=normalized_ready_label,
    )


def admission_from_enqueue_reason(
    *,
    raw_reason: object,
    fallback_reason: ExecutionAdmissionReason = ExecutionAdmissionReason.EXECUTION_BLOCKED,
) -> ExecutionAdmissionDecision:
    parsed_reason = parse_execution_admission_reason(raw_reason)
    if parsed_reason is not None:
        return build_execution_admission_block(reason=parsed_reason)
    detail = str(getattr(raw_reason, "value", raw_reason) or "").strip() or None
    return build_execution_admission_block(reason=fallback_reason, detail=detail)


def resolve_run_gate_block(*, decision_result: object) -> RunGateBlock | None:
    execution_gate = getattr(decision_result, "execution_gate", None)
    gate_state = getattr(execution_gate, "state", None)
    gate_reason = getattr(execution_gate, "reason", None)
    if gate_state == ExecutionGateState.ALLOW_EXECUTION:
        return None
    if gate_reason is None:
        fallback_reason = PrecheckOutcome.parse(
            getattr(getattr(decision_result, "decision", None), "block_reason", None)
        )
        if gate_state == ExecutionGateState.POLICY_ERROR:
            gate_reason = execution_gate_reason_for_precheck_outcome(
                outcome=PrecheckOutcome.POLICY_EVAL_FAILED,
            )
        elif gate_state == ExecutionGateState.BLOCK_READY_LABEL:
            gate_reason = execution_gate_reason_for_precheck_outcome(
                outcome=PrecheckOutcome.MISSING_READY_LABEL,
            )
        else:
            gate_reason = execution_gate_reason_for_precheck_outcome(
                outcome=fallback_reason or PrecheckOutcome.DECISION_GATE_REQUIRED,
            )
    return RunGateBlock(
        reason=str(getattr(gate_reason, "reason_code", "") or "").strip(),
        guidance=str(getattr(gate_reason, "guidance", "") or "").strip(),
        detail=str(getattr(gate_reason, "detail", "") or "").strip() or None,
        ready_label=str(getattr(gate_reason, "ready_label", "") or "").strip() or None,
    )


def resolve_execution_admission(*, decision_result: object) -> ExecutionAdmissionDecision:
    gate_block = resolve_run_gate_block(decision_result=decision_result)
    pre_check = getattr(getattr(decision_result, "decision", None), "pre_check", None)
    parsed_precheck_outcome = PrecheckOutcome.parse(getattr(pre_check, "outcome", None))
    precheck_outcome = parsed_precheck_outcome.value if parsed_precheck_outcome is not None else None
    required_worker_capability = str(
        getattr(pre_check, "required_worker_capability", "") or ""
    ).strip() or None
    if gate_block is None:
        return ExecutionAdmissionDecision(
            can_enqueue=True,
            precheck_outcome=precheck_outcome,
            required_worker_capability=required_worker_capability,
        )
    return ExecutionAdmissionDecision(
        can_enqueue=False,
        precheck_outcome=precheck_outcome,
        required_worker_capability=required_worker_capability,
        reason=_parse_admission_reason(gate_block.reason),
        guidance=gate_block.guidance,
        detail=gate_block.detail,
        ready_label=gate_block.ready_label,
    )


def resolve_worker_decision_from_precheck(*, pre_check: object) -> WorkerDecision:
    parsed_outcome = PrecheckOutcome.parse(getattr(pre_check, "outcome", None))
    if parsed_outcome is None:
        return WorkerDecision(
            allowed=False,
            decision_gate=None,
            configuration_error="Execution readiness check failed: persisted policy evaluation failure",
            block_reason=PrecheckOutcome.POLICY_EVAL_FAILED.value,
            pre_check=pre_check,
        )
    if parsed_outcome is PrecheckOutcome.READY_FOR_AGENT:
        return WorkerDecision(
            allowed=True,
            decision_gate=None,
            configuration_error=None,
            classification=DecisionClassification.CLEAR,
            pre_check=pre_check,
        )
    if parsed_outcome is PrecheckOutcome.POLICY_EVAL_FAILED:
        return WorkerDecision(
            allowed=False,
            decision_gate=None,
            configuration_error="Execution readiness check failed: persisted policy evaluation failure",
            block_reason=PrecheckOutcome.POLICY_EVAL_FAILED.value,
            pre_check=pre_check,
        )

    ready_label = str(getattr(pre_check, "ready_label", "") or "").strip() or None
    guidance = guidance_for_precheck_block_reason(
        block_reason=parsed_outcome.value,
        ready_label=ready_label,
    )
    precheck_gate = getattr(pre_check, "decision_gate", None)
    if parsed_outcome in {
        PrecheckOutcome.DECISION_GATE_REQUIRED,
        PrecheckOutcome.GTD_REQUIRED,
        PrecheckOutcome.EXECUTION_BLOCKED,
    } and isinstance(precheck_gate, DecisionGateResult) and str(precheck_gate.reason or "").strip():
        return WorkerDecision(
            allowed=False,
            decision_gate=precheck_gate,
            configuration_error=None,
            block_reason=parsed_outcome.value,
            classification=(
                DecisionClassification.DECISION_GATE
                if parsed_outcome is PrecheckOutcome.DECISION_GATE_REQUIRED
                else DecisionClassification.GTD
            ),
            pre_check=pre_check,
        )

    synthetic_gate = DecisionGateResult(
        triggered=True,
        reason=guidance,
        missing_sections=tuple(getattr(getattr(pre_check, "gtd", None), "missing_criteria", ()) or ()),
        questions=tuple(getattr(getattr(pre_check, "gtd", None), "clarification_questions", ()) or ()),
        recommendation="Resolve execution-readiness blockers before execution.",
        tags=(),
    )
    return WorkerDecision(
        allowed=False,
        decision_gate=synthetic_gate,
        configuration_error=None,
        block_reason=parsed_outcome.value,
        classification=(
            DecisionClassification.CLEAR
            if parsed_outcome is PrecheckOutcome.MISSING_READY_LABEL
            else (
                DecisionClassification.DECISION_GATE
                if parsed_outcome is PrecheckOutcome.DECISION_GATE_REQUIRED
                else DecisionClassification.GTD
            )
        ),
        pre_check=pre_check,
    )


def resolve_worker_blocked_outcome(*, worker_decision: object) -> WorkerBlockedOutcome:
    pre_check = getattr(worker_decision, "pre_check", None)
    decision_gate_reason = str(getattr(getattr(worker_decision, "decision_gate", None), "reason", "") or "").strip()
    parsed_block_reason = PrecheckOutcome.parse(getattr(worker_decision, "block_reason", None))
    block_reason = parsed_block_reason.value if parsed_block_reason is not None else None
    ready_label = str(getattr(pre_check, "ready_label", "") or "").strip() or None
    parsed_pre_check_outcome = PrecheckOutcome.parse(getattr(pre_check, "outcome", None))
    pre_check_outcome = parsed_pre_check_outcome.value if parsed_pre_check_outcome is not None else block_reason

    if parsed_block_reason is PrecheckOutcome.MISSING_READY_LABEL:
        reason = decision_gate_reason or (
            f"Issue is missing the configured ready label. ({ready_label})"
            if ready_label
            else "Issue is missing the configured ready label."
        )
        next_steps = (
            (f"Apply ready label `{ready_label}` to the Jira issue, then retry the run.",)
            if ready_label
            else ("Apply the configured ready label to the Jira issue, then retry the run.",)
        )
        return WorkerBlockedOutcome(
            reason=reason,
            next_steps=next_steps,
            ready_label=ready_label,
            block_reason=block_reason,
            pre_check_outcome=pre_check_outcome,
        )

    if not decision_gate_reason:
        raise ValueError("missing decision gate reason for blocked worker decision")
    return WorkerBlockedOutcome(
        reason=decision_gate_reason,
        next_steps=("Reply with the required clarification on the issue, then retry the run.",),
        ready_label=ready_label,
        block_reason=block_reason,
        pre_check_outcome=pre_check_outcome,
    )
