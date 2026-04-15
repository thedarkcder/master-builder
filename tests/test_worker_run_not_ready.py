from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator.core.worker.run_not_ready import derive_run_not_ready_outcome


def _worker_decision(
    *,
    block_reason: str | None,
    decision_gate_reason: str,
    ready_label: str = "agent:ready",
    pre_check_outcome: str = "decision_gate_required",
) -> object:
    return SimpleNamespace(
        block_reason=block_reason,
        decision_gate=SimpleNamespace(reason=decision_gate_reason),
        pre_check=SimpleNamespace(
            ready_label=ready_label,
            outcome=pre_check_outcome,
        ),
    )


def test_derive_run_not_ready_outcome_missing_ready_label() -> None:
    outcome = derive_run_not_ready_outcome(
        worker_decision=_worker_decision(
            block_reason="missing_ready_label",
            decision_gate_reason="Issue is missing the configured ready label. (agent:ready)",
            pre_check_outcome="missing_ready_label",
        )
    )
    assert outcome.block_reason == "missing_ready_label"
    assert outcome.ready_label == "agent:ready"
    assert "agent:ready" in outcome.reason
    assert len(outcome.next_steps) == 1


def test_derive_run_not_ready_outcome_requires_decision_gate_reason() -> None:
    with pytest.raises(ValueError, match="missing decision gate reason"):
        derive_run_not_ready_outcome(
            worker_decision=_worker_decision(
                block_reason="decision_gate_required",
                decision_gate_reason="",
            )
        )


def test_derive_run_not_ready_outcome_uses_decision_gate_reason() -> None:
    outcome = derive_run_not_ready_outcome(
        worker_decision=_worker_decision(
            block_reason="decision_gate_required",
            decision_gate_reason="Need dependencies and risks.",
        )
    )
    assert outcome.reason == "Need dependencies and risks."
    assert outcome.pre_check_outcome == "decision_gate_required"


def test_run_not_ready_outcome_round_trips_through_canonical_payload() -> None:
    outcome = derive_run_not_ready_outcome(
        worker_decision=_worker_decision(
            block_reason="missing_ready_label",
            decision_gate_reason="Issue is missing the configured ready label. (agent:ready)",
            pre_check_outcome="missing_ready_label",
        )
    )

    payload = outcome.dump()

    assert payload["reason"] == outcome.reason
    assert payload["next_steps"] == list(outcome.next_steps)
    assert payload["ready_label"] == "agent:ready"
