from __future__ import annotations

from types import SimpleNamespace

from orchestrator.core.decision.state_machine import decision_missing_slots_for_precheck


def test_precheck_missing_slots_keeps_exact_canonical_keys_only() -> None:
    pre_check = SimpleNamespace(
        decision_gate=SimpleNamespace(missing_sections=("decision_owner",)),
        gtd_missing_criteria=("dependencies_and_risks",),
    )

    assert decision_missing_slots_for_precheck(pre_check) == [
        "decision_owner",
        "dependencies_and_risks",
    ]


def test_precheck_missing_slots_rejects_alias_and_noncanonical_keys() -> None:
    pre_check = SimpleNamespace(
        decision_gate=SimpleNamespace(
            missing_sections=("decision owner", "Decision_Owner")
        ),
        gtd_missing_criteria=("dependencies / risks", "Dependencies_And_Risks"),
    )

    assert decision_missing_slots_for_precheck(pre_check) == []
