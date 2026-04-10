from __future__ import annotations

from dataclasses import dataclass

from orchestrator.core.communications.enqueue_reason_contract import enqueue_reason_guidance
from orchestrator.core.decision_types import PrecheckOutcome


@dataclass(frozen=True)
class RunNotReadyOutcome:
    reason: str
    next_steps: tuple[str, ...]
    ready_label: str | None
    block_reason: str | None
    pre_check_outcome: str | None


def derive_run_not_ready_outcome(*, worker_decision: object) -> RunNotReadyOutcome:
    pre_check = getattr(worker_decision, "pre_check", None)
    parsed_block_reason = PrecheckOutcome.parse(getattr(worker_decision, "block_reason", None))
    block_reason = parsed_block_reason.value if parsed_block_reason is not None else None
    ready_label = str(getattr(pre_check, "ready_label", "") or "").strip() or None
    parsed_pre_check_outcome = PrecheckOutcome.parse(getattr(pre_check, "outcome", None))
    pre_check_outcome = parsed_pre_check_outcome.value if parsed_pre_check_outcome is not None else None

    if parsed_block_reason is PrecheckOutcome.MISSING_READY_LABEL:
        reason = (
            f"{enqueue_reason_guidance('missing_ready_label')} ({ready_label})"
            if ready_label
            else enqueue_reason_guidance("missing_ready_label")
        )
        next_steps = (
            (f"Apply ready label `{ready_label}` to the Jira issue, then retry the run.",)
            if ready_label
            else ("Apply the configured ready label to the Jira issue, then retry the run.",)
        )
        return RunNotReadyOutcome(
            reason=reason,
            next_steps=next_steps,
            ready_label=ready_label,
            block_reason=block_reason,
            pre_check_outcome=pre_check_outcome,
        )

    decision_gate_reason = (
        str(getattr(getattr(worker_decision, "decision_gate", None), "reason", "") or "").strip()
    )
    if not decision_gate_reason:
        raise ValueError("missing decision gate reason for blocked worker decision")
    return RunNotReadyOutcome(
        reason=decision_gate_reason,
        next_steps=("Reply with the required clarification on the issue, then retry the run.",),
        ready_label=ready_label,
        block_reason=block_reason,
        pre_check_outcome=pre_check_outcome,
    )
