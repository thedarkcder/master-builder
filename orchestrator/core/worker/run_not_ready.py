from __future__ import annotations

from dataclasses import dataclass

from orchestrator.core.decision_state_machine import WorkerBlockedOutcome
from orchestrator.core.decision_state_machine import resolve_worker_blocked_outcome


@dataclass(frozen=True)
class RunNotReadyOutcome(WorkerBlockedOutcome):
    pass


def derive_run_not_ready_outcome(*, worker_decision: object) -> RunNotReadyOutcome:
    outcome = resolve_worker_blocked_outcome(worker_decision=worker_decision)
    return RunNotReadyOutcome(
        reason=outcome.reason,
        next_steps=outcome.next_steps,
        ready_label=outcome.ready_label,
        block_reason=outcome.block_reason,
        pre_check_outcome=outcome.pre_check_outcome,
    )
