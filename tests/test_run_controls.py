from types import SimpleNamespace

from orchestrator.api.discord.commands.run_controls import (
    DECISION_GATE_BLOCK_END,
    DECISION_GATE_BLOCK_START,
    _choose_updated_summary,
    _locked_decision_gate_reason,
    _upsert_decision_gate_clarifications_block,
)


def test_upsert_decision_gate_clarifications_preserves_existing_description() -> None:
    existing = "Current ticket intro.\n\nObjective: existing objective."
    block = "\n".join(
        [
            DECISION_GATE_BLOCK_START,
            "## Decision Gate Clarifications",
            "Objective: clarified objective",
            DECISION_GATE_BLOCK_END,
        ]
    )
    merged = _upsert_decision_gate_clarifications_block(current_description=existing, block=block)
    assert "Current ticket intro." in merged
    assert "Objective: existing objective." in merged
    assert "## Decision Gate Clarifications" in merged
    assert merged.count(DECISION_GATE_BLOCK_START) == 1


def test_upsert_decision_gate_clarifications_replaces_existing_block() -> None:
    initial = "\n".join(
        [
            "Ticket intro.",
            DECISION_GATE_BLOCK_START,
            "## Decision Gate Clarifications",
            "Objective: old",
            DECISION_GATE_BLOCK_END,
            "Trailing notes.",
        ]
    )
    replacement = "\n".join(
        [
            DECISION_GATE_BLOCK_START,
            "## Decision Gate Clarifications",
            "Objective: new",
            DECISION_GATE_BLOCK_END,
        ]
    )
    merged = _upsert_decision_gate_clarifications_block(current_description=initial, block=replacement)
    assert "Objective: new" in merged
    assert "Objective: old" not in merged
    assert merged.count(DECISION_GATE_BLOCK_START) == 1
    assert "Trailing notes." in merged


def test_choose_updated_summary_avoids_large_rewrite() -> None:
    updated = _choose_updated_summary(
        issue_key="GP-80",
        current_summary="Keep original summary wording",
        suggested_summary="Completely different rewritten summary text",
    )
    assert updated == "Keep original summary wording | DG clarified"


def test_locked_decision_gate_reason_ignored_for_gtd_classification() -> None:
    decision_gate = SimpleNamespace(reason="Decision Gate not required.")
    assert _locked_decision_gate_reason(classification="gtd", decision_gate=decision_gate) is None


def test_locked_decision_gate_reason_present_for_decision_gate_classification() -> None:
    decision_gate = SimpleNamespace(reason="  Clarification is required  ")
    assert (
        _locked_decision_gate_reason(classification="decision_gate", decision_gate=decision_gate)
        == "Clarification is required"
    )
