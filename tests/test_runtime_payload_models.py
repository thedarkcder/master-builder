from __future__ import annotations

import pytest

from orchestrator.core.runtime_payload_models import (
    DecisionPlannerPayload,
    PrecheckMessagePayload,
    PrecheckPolicyPayload,
)


def test_precheck_policy_payload_requires_reason_and_recommendation() -> None:
    payload = PrecheckPolicyPayload.from_payload(
        {
            "triggered": False,
            "reason": "Decision Gate not required",
            "missing_sections": [],
            "questions": [],
            "recommendation": "Proceed",
            "tags": [],
            "gtd_valid": True,
            "gtd_missing_criteria": [],
            "gtd_clarification_questions": [],
        }
    )
    assert payload.decision_gate_reason == "Decision Gate not required"
    assert payload.gtd_valid is True


def test_precheck_policy_payload_rejects_invalid_gtd_without_questions() -> None:
    with pytest.raises(RuntimeError, match="invalid GTD result"):
        PrecheckPolicyPayload.from_payload(
            {
                "triggered": False,
                "reason": "Decision Gate not required",
                "missing_sections": [],
                "questions": [],
                "recommendation": "Proceed",
                "tags": [],
                "gtd_valid": False,
                "gtd_missing_criteria": ["how_to_test"],
                "gtd_clarification_questions": [],
            }
        )


def test_decision_planner_payload_normalizes_questions_and_states() -> None:
    payload = DecisionPlannerPayload.from_payload(
        {
            "gate_status": "blocked_decision_gate",
            "reason": "Need owner decision",
            "questions": [
                {"question_id": "dg_1", "kind": "decision_gate", "question": "Who owns this?", "status": "open"}
            ],
            "question_states": [],
            "resolved_items": [],
            "missing_items": ["decision_owner"],
        },
        classification="decision_gate",
    )
    assert payload.gate_status == "blocked_decision_gate"
    assert len(payload.question_states) == 1
    assert payload.missing_items == ("decision_owner",)


def test_precheck_message_payload_rejects_invalid_classification() -> None:
    with pytest.raises(RuntimeError, match="invalid classification"):
        PrecheckMessagePayload.from_payload(
            {
                "message": "Need clarification",
                "questions": [],
                "classification": "invalid",
            }
        )
