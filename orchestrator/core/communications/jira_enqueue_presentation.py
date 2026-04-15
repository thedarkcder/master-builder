from __future__ import annotations

from dataclasses import dataclass

from orchestrator.core.communications.enqueue_reason_contract import enqueue_reason_guidance
from orchestrator.core.decision_engine import DecisionEngineResult
from orchestrator.core.decision_state_machine import ExecutionAdmissionDecision
from orchestrator.core.decision_types import PrecheckOutcome


@dataclass(frozen=True)
class BacklogPreRunCheckPresentation:
    outcome: PrecheckOutcome
    ready_label: str | None = None
    decision_gate_reason: str | None = None
    gtd_missing_criteria: tuple[str, ...] = ()
    required_worker_label: str | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "outcome": self.outcome.value,
            "ready_label": self.ready_label,
            "decision_gate_reason": self.decision_gate_reason,
            "gtd_missing_criteria": list(self.gtd_missing_criteria),
            "required_worker_label": self.required_worker_label,
        }


def build_backlog_pre_run_check_presentation(
    *,
    decision_result: DecisionEngineResult,
    ready_label: str | None,
    policy_error_reason: str = "Precheck policy evaluation failed",
) -> BacklogPreRunCheckPresentation:
    pre_check = decision_result.decision.pre_check
    if pre_check is None:
        return BacklogPreRunCheckPresentation(
            outcome=PrecheckOutcome.POLICY_EVAL_FAILED,
            ready_label=ready_label,
            decision_gate_reason=policy_error_reason,
        )
    decision_gate_reason = normalize_backlog_pre_run_check_text(pre_check.decision_gate_reason)
    parsed_outcome = PrecheckOutcome.parse(pre_check.outcome) or PrecheckOutcome.POLICY_EVAL_FAILED
    return BacklogPreRunCheckPresentation(
        outcome=parsed_outcome,
        ready_label=pre_check.ready_label,
        decision_gate_reason=decision_gate_reason,
        gtd_missing_criteria=tuple(
            str(item).strip() for item in pre_check.gtd_missing_criteria if str(item).strip()
        ),
        required_worker_label=pre_check.required_worker_label,
    )


def normalize_backlog_pre_run_check_text(raw_value: str | None, *, max_chars: int = 240) -> str | None:
    if not isinstance(raw_value, str):
        return None
    normalized = " ".join(raw_value.strip().split())
    if not normalized:
        return None
    if len(normalized) > max_chars:
        if max_chars <= 3:
            return normalized[:max_chars]
        return f"{normalized[: max_chars - 3].rstrip()}..."
    return normalized


def format_jira_enqueue_skipped_message(
    *,
    issue_key: str,
    issue_status: str | None,
    admission: ExecutionAdmissionDecision,
    extra_detail: str | None = None,
) -> str:
    detail = f" ({extra_detail})" if extra_detail else ""
    guidance = enqueue_reason_guidance(admission.reason_code)
    return (
        f"Jira webhook did not queue a run for `{issue_key}`.\n"
        f"Reason: `{admission.reason_code}`{detail}\n"
        f"Guidance: {guidance}\n"
        f"Status: `{issue_status or 'unknown'}`"
    )


def format_backlog_pre_run_check_message(
    *,
    issue_key: str,
    board_id: int,
    issue_status: str | None,
    pre_run_check: BacklogPreRunCheckPresentation,
) -> str:
    outcome = pre_run_check.outcome
    ready_label = pre_run_check.ready_label
    decision_gate_reason = normalize_backlog_pre_run_check_text(pre_run_check.decision_gate_reason)
    gtd_missing_criteria = list(pre_run_check.gtd_missing_criteria)
    lines = [
        f"New issue `{issue_key}` was added to the backlog on board `{board_id}`.",
        "Run was not started (backlog-only event).",
    ]
    if issue_status:
        lines.append(f"Issue status: `{issue_status}`")
    if outcome is PrecheckOutcome.READY_FOR_AGENT:
        if isinstance(ready_label, str) and ready_label.strip():
            lines.append(f"Pre-run check: labeled `{ready_label.strip()}` and ready for agent.")
        else:
            lines.append("Pre-run check: ready for agent.")
    elif outcome is PrecheckOutcome.DECISION_GATE_REQUIRED:
        lines.append("Pre-run check: Decision Gate required before execution.")
        if decision_gate_reason:
            lines.append(f"Decision Gate reason: {decision_gate_reason}")
    elif outcome is PrecheckOutcome.GTD_REQUIRED:
        lines.append("Pre-run check: Good To Do details are incomplete.")
        if gtd_missing_criteria:
            lines.append("Missing GTD criteria: " + ", ".join(gtd_missing_criteria))
    elif outcome is PrecheckOutcome.MISSING_READY_LABEL:
        if isinstance(ready_label, str) and ready_label.strip():
            lines.append(f"Pre-run check: missing ready label `{ready_label.strip()}`.")
        else:
            lines.append("Pre-run check: missing ready label.")
    required_worker_label = str(pre_run_check.required_worker_label or "").strip()
    if required_worker_label:
        lines.append(f"Required worker capability: `{required_worker_label}`.")
    return "\n".join(lines)
