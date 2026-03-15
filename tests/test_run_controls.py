from types import SimpleNamespace

from orchestrator.api.discord.commands.run_controls import _locked_decision_gate_reason


def test_locked_decision_gate_reason_ignored_for_gtd_classification() -> None:
    decision_gate = SimpleNamespace(reason="Decision Gate not required.")
    assert _locked_decision_gate_reason(classification="gtd", decision_gate=decision_gate) is None


def test_locked_decision_gate_reason_present_for_decision_gate_classification() -> None:
    decision_gate = SimpleNamespace(reason="  Clarification is required  ")
    assert (
        _locked_decision_gate_reason(classification="decision_gate", decision_gate=decision_gate)
        == "Clarification is required"
    )
