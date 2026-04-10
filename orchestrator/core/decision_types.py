from __future__ import annotations

from dataclasses import dataclass, field, replace
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

def blocking_reason_for_precheck(pre_check: object) -> str | None:
    return blocking_reason_for_outcome(getattr(pre_check, "outcome", None))


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
    classification: DecisionClassification | None = None
    pre_check: PreRunCheckResult | None = None


class DecisionClassification(str, Enum):
    CLEAR = "clear"
    DECISION_GATE = "decision_gate"
    GTD = "gtd"
    BOTH = "both"

    @classmethod
    def parse(cls, value: object) -> DecisionClassification:
        normalized = str(getattr(value, "value", value) or "").strip().lower()
        if normalized == cls.DECISION_GATE.value:
            return cls.DECISION_GATE
        if normalized == cls.GTD.value:
            return cls.GTD
        if normalized == cls.BOTH.value:
            return cls.BOTH
        return cls.CLEAR

    @property
    def blocks_execution(self) -> bool:
        return self in {self.DECISION_GATE, self.GTD, self.BOTH}

    @property
    def includes_decision_gate(self) -> bool:
        return self in {self.DECISION_GATE, self.BOTH}

    @property
    def includes_gtd(self) -> bool:
        return self in {self.GTD, self.BOTH}


class DecisionQuestionKind(str, Enum):
    DECISION_GATE = "decision_gate"
    GTD = "gtd"

    @classmethod
    def parse(cls, value: object) -> DecisionQuestionKind:
        normalized = str(getattr(value, "value", value) or "").strip().lower()
        if normalized == cls.GTD.value:
            return cls.GTD
        return cls.DECISION_GATE


class PrecheckOutcome(str, Enum):
    READY_FOR_AGENT = "ready_for_agent"
    DECISION_GATE_REQUIRED = "decision_gate_required"
    GTD_REQUIRED = "gtd_required"
    EXECUTION_BLOCKED = "execution_blocked"
    MISSING_READY_LABEL = "missing_ready_label"
    POLICY_EVAL_FAILED = "policy_eval_failed"

    @classmethod
    def parse(cls, value: object) -> PrecheckOutcome | None:
        normalized = str(getattr(value, "value", value) or "").strip().lower()
        for item in cls:
            if normalized == item.value:
                return item
        return None

    @property
    def is_decision_block(self) -> bool:
        return self in {self.DECISION_GATE_REQUIRED, self.GTD_REQUIRED, self.EXECUTION_BLOCKED}


class JiraConfigKey(str, Enum):
    CONNECTION_ID = "connection_id"
    PROJECT_KEYS = "project_keys"
    READY_STATUSES = "ready_statuses"
    READY_LABEL = "ready_label"
    READY_TRIGGER_MODE = "ready_trigger_mode"


class ReadinessState(str, Enum):
    READY = "ready"
    BLOCKED_DECISION = "blocked_decision"
    BLOCKED_READY_LABEL = "blocked_ready_label"
    POLICY_ERROR = "policy_error"


@dataclass(frozen=True)
class ReadinessDecision:
    state: ReadinessState
    reason_code: PrecheckOutcome | None = None


def blocking_reason_for_outcome(outcome: object) -> str | None:
    parsed = PrecheckOutcome.parse(outcome)
    if parsed is None or not parsed.is_decision_block and parsed is not PrecheckOutcome.MISSING_READY_LABEL:
        return None
    return parsed.value


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


def tenant_ready_label(tenant: object | None) -> str | None:
    return tenant_jira_config_text(tenant=tenant, key=JiraConfigKey.READY_LABEL)


def tenant_jira_config_text(*, tenant: object | None, key: JiraConfigKey) -> str | None:
    if tenant is None:
        return None
    jira_config = getattr(tenant, "jira_config", None)
    if not isinstance(jira_config, dict):
        return None
    raw_value = jira_config.get(key.value)
    if not isinstance(raw_value, str):
        return None
    normalized = raw_value.strip()
    return normalized or None


def tenant_jira_project_keys(tenant: object | None) -> tuple[str, ...]:
    if tenant is None:
        return ()
    jira_config = getattr(tenant, "jira_config", None)
    if not isinstance(jira_config, dict):
        return ()
    raw_project_keys = jira_config.get(JiraConfigKey.PROJECT_KEYS.value)
    if not isinstance(raw_project_keys, list):
        return ()
    return tuple(
        normalized
        for normalized in (str(value).strip() for value in raw_project_keys)
        if normalized
    )


def tenant_jira_ready_statuses(tenant: object | None) -> tuple[str, ...]:
    if tenant is None:
        return ()
    jira_config = getattr(tenant, "jira_config", None)
    if not isinstance(jira_config, dict):
        return ()
    raw_ready_statuses = jira_config.get(JiraConfigKey.READY_STATUSES.value)
    if not isinstance(raw_ready_statuses, list):
        return ()
    return tuple(
        normalized
        for normalized in (str(value).strip() for value in raw_ready_statuses)
        if normalized
    )


def tenant_ready_trigger_mode(tenant: object | None) -> str:
    raw_mode = tenant_jira_config_text(tenant=tenant, key=JiraConfigKey.READY_TRIGGER_MODE)
    if raw_mode is None:
        return "status_recheck"
    normalized_mode = raw_mode.lower()
    if normalized_mode in {"status_recheck", "transition_only"}:
        return normalized_mode
    return "status_recheck"


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
class ExecutionGateResolution:
    state: ExecutionGateState
    reason: ExecutionGateReason | None = None


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
    classification: DecisionClassification
    missing_slots: list[str]
    auto_resolved_slots: list[str]
    case_id: str
    case_state: str
    cycle_id: str | None
    outbox_effect_ids: tuple[str, ...]
    duplicate_event: bool
    execution_gate: ExecutionGateResolution = field(
        default_factory=lambda: ExecutionGateResolution(state=ExecutionGateState.ALLOW_EXECUTION)
    )

    @property
    def execution_gate_state(self) -> ExecutionGateState:
        return self.execution_gate.state

    @property
    def execution_gate_reason(self) -> ExecutionGateReason | None:
        return self.execution_gate.reason

    @property
    def classification_code(self) -> str:
        return self.classification.value


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
            reason=ExecutionGateReason(
                reason_code="policy_eval_failed",
                guidance=enqueue_reason_guidance("policy_eval_failed"),
                detail=str(decision.policy_error or "").strip() or None,
            ),
        )

    if readiness.state == ReadinessState.BLOCKED_READY_LABEL:
        ready_label = str(getattr(decision.pre_check, "ready_label", "") or "").strip() or None
        guidance = (
            f"{enqueue_reason_guidance('missing_ready_label')} ({ready_label})"
            if ready_label
            else enqueue_reason_guidance("missing_ready_label")
        )
        return ExecutionGateResolution(
            state=ExecutionGateState.BLOCK_READY_LABEL,
            reason=ExecutionGateReason(
                reason_code="missing_ready_label",
                guidance=guidance,
                ready_label=ready_label,
            ),
        )

    if readiness.state == ReadinessState.BLOCKED_DECISION:
        detail = str(getattr(decision.pre_check, "decision_gate_reason", "") or "").strip() or None
        reason_code = (readiness.reason_code or PrecheckOutcome.DECISION_GATE_REQUIRED).value
        return ExecutionGateResolution(
            state=ExecutionGateState.BLOCK_DECISION,
            reason=ExecutionGateReason(
                reason_code=reason_code,
                guidance=enqueue_reason_guidance(reason_code),
                detail=detail,
            ),
        )

    return ExecutionGateResolution(state=ExecutionGateState.ALLOW_EXECUTION)
