from __future__ import annotations

import pytest

from orchestrator.core.runtime.payload_models import (
    ArchitectStageOutput,
    ChildTicketSpec,
    DecisionPlanner,
    DesignPlanning,
    EngineeringSeedPlan,
    PlannerGateStatus,
    PmParentSeedPlan,
    PMDecisionRequest,
    PMDecisionResolutionSet,
    PMInterviewPlan,
    RuntimeMessageBrief,
    RuntimeMessage,
    PrecheckMessage,
    PrecheckPolicy,
    StakeholderEscalation,
    TechnicalDecision,
)


def _pm_brief_payload() -> dict[str, object]:
    return {
        "objective": "Improve reliability",
        "user_value": "Operators trust failed work recovery.",
        "acceptance_criteria": ["Retries are visible."],
        "scope_in": ["Workflow retry visibility"],
        "scope_out": [],
        "constraints": [],
        "risks": [],
        "success_outcomes": ["Fewer stuck executions"],
        "recommendation": "Proceed with bounded retry evidence.",
        "open_questions": [],
        "next_steps": [],
    }


def _technical_decision_payload() -> dict[str, object]:
    return {
        "decision_id": "decision-1",
        "area": "architecture",
        "question": "Postgres RLS or app-layer authorization?",
        "options": [
            {
                "option_id": "rls",
                "title": "Postgres RLS",
                "description": "Enforce tenant visibility in the database.",
                "benefits": ["Database-owned isolation"],
                "risks": ["Requires migration discipline"],
                "rejected_reason": "",
            },
            {
                "option_id": "app-only",
                "title": "Application-only authorization",
                "description": "Apply tenant filters only in application queries.",
                "benefits": ["Simpler migrations"],
                "risks": ["One missing filter leaks data"],
                "rejected_reason": "It does not provide a database backstop.",
            },
        ],
        "selected_option_id": "rls",
        "rationale": "Tenant isolation must be enforced below application code.",
        "evidence": ["Tenant-scoped tables contain sensitive workflow data"],
        "confidence": "high",
        "product_impact": "none",
    }


def test_precheck_policy_payload_requires_reason_and_recommendation() -> None:
    payload = PrecheckPolicy.from_payload(
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
        PrecheckPolicy.from_payload(
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
    payload = DecisionPlanner.from_payload(
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
    assert payload.gate_status is PlannerGateStatus.BLOCKED_DECISION_GATE
    assert len(payload.question_states) == 1
    assert payload.missing_items == ("decision_owner",)


def test_decision_planner_payload_rejects_invalid_question_status() -> None:
    with pytest.raises(RuntimeError, match="Decision planner question has invalid status"):
        DecisionPlanner.from_payload(
            {
                "gate_status": "blocked_decision_gate",
                "reason": "Need owner decision",
                "questions": [
                    {"question_id": "dg_1", "kind": "decision_gate", "question": "Who owns this?", "status": "later"}
                ],
                "question_states": [],
                "resolved_items": [],
                "missing_items": ["decision_owner"],
            },
            classification="decision_gate",
        )


def test_decision_planner_payload_rejects_non_object_question() -> None:
    with pytest.raises(RuntimeError, match="Decision planner questions\\[1\\] must be an object"):
        DecisionPlanner.from_payload(
            {
                "gate_status": "blocked_decision_gate",
                "reason": "Need owner decision",
                "questions": ["Who owns this?"],
                "question_states": [],
                "resolved_items": [],
                "missing_items": ["decision_owner"],
            },
            classification="decision_gate",
        )


def test_precheck_policy_payload_rejects_invalid_missing_sections_type() -> None:
    with pytest.raises(RuntimeError, match="missing_sections has invalid list"):
        PrecheckPolicy.from_payload(
            {
                "triggered": False,
                "reason": "Decision Gate not required",
                "missing_sections": "none",
                "questions": [],
                "recommendation": "Proceed",
                "tags": [],
                "gtd_valid": True,
                "gtd_missing_criteria": [],
                "gtd_clarification_questions": [],
            }
        )


def test_precheck_message_payload_rejects_invalid_classification() -> None:
    with pytest.raises(RuntimeError, match="invalid classification"):
        PrecheckMessage.from_payload(
            {
                "message": "Need clarification",
                "questions": [],
                "classification": "invalid",
            }
        )


def test_precheck_message_payload_rejects_invalid_questions_type() -> None:
    with pytest.raises(RuntimeError, match="Precheck message payload questions has invalid list"):
        PrecheckMessage.from_payload(
            {
                "message": "Need clarification",
                "questions": "none",
                "classification": "gtd",
            }
        )


def test_child_ticket_spec_payload_requires_done_means() -> None:
    with pytest.raises(
        RuntimeError,
        match="engineering_planning child_ticket_specs\\[1\\] with empty done_means; expected a non-empty array of strings",
    ):
        ChildTicketSpec.from_payload(
            planning_state="engineering_planning",
            issue_index=1,
            raw_value={
                "summary": "Create invite service",
                "capability": "Invite creation",
                "delivery": "Build invite flow",
                "expected_outcome": "Users can invite friends",
                "acceptance_criteria": ["Invite link can be created"],
                "how_to_test": ["Run invite integration test"],
                "done_means": [],
            },
        )


def test_child_ticket_spec_payload_rejects_invalid_optional_lists() -> None:
    with pytest.raises(RuntimeError, match="Child ticket spec payload has invalid dependencies"):
        ChildTicketSpec.from_payload(
            planning_state="engineering_planning",
            issue_index=1,
            raw_value={
                "summary": "Create invite service",
                "capability": "Invite creation",
                "delivery": "Build invite flow",
                "expected_outcome": "Users can invite friends",
                "acceptance_criteria": ["Invite link can be created"],
                "how_to_test": ["Run invite integration test"],
                "done_means": ["Automated tests pass"],
                "dependencies": "none",
            },
        )


def test_architect_stage_output_payload_rejects_missing_child_ticket_specs() -> None:
    with pytest.raises(RuntimeError, match="engineering_planning missing child_ticket_specs"):
        ArchitectStageOutput.from_payload(
            planning_state="engineering_planning",
            persona_id="architect",
            role_label="Architect",
            payload={
                "findings": ["Invite creation needs a service boundary"],
                "recommendations": ["Create invite service"],
                "required_tasks": ["Build invite service"],
                "technical_decisions": [_technical_decision_payload()],
                "pm_decision_requests": [],
                "acceptance_impacts": ["Invite flow works"],
            },
        )


def test_technical_decision_payload_round_trips_selected_recommendation() -> None:
    payload = TechnicalDecision.from_payload(
        _technical_decision_payload(),
        context="engineering_planning technical_decisions[1]",
    )

    assert payload.selected_option_id == "rls"
    assert payload.confidence == "high"
    assert payload.product_impact == "none"
    assert payload.to_payload()["options"][0]["option_id"] == "rls"


def test_technical_decision_payload_rejects_unknown_selected_option() -> None:
    raw_payload = _technical_decision_payload()
    raw_payload["selected_option_id"] = "missing"

    with pytest.raises(RuntimeError, match="selected_option_id does not match"):
        TechnicalDecision.from_payload(
            raw_payload,
            context="engineering_planning technical_decisions[1]",
        )


def test_pm_decision_request_payload_requires_related_decision_ids() -> None:
    request = PMDecisionRequest.from_payload(
        {
            "request_id": "pm-decision-1",
            "question": "Should customers see this as an acceptance promise?",
            "why_it_matters": "The PM owns acceptance interpretation.",
            "related_decision_ids": ["decision-1"],
        },
        context="security_planning pm_decision_requests[1]",
    )

    assert request.request_id == "pm-decision-1"
    assert request.to_payload()["related_decision_ids"] == ["decision-1"]


def test_pm_decision_request_payload_rejects_empty_related_decision_ids() -> None:
    with pytest.raises(RuntimeError, match="missing related_decision_ids"):
        PMDecisionRequest.from_payload(
            {
                "request_id": "pm-decision-1",
                "question": "Should customers see this as an acceptance promise?",
                "why_it_matters": "The PM owns acceptance interpretation.",
                "related_decision_ids": [],
            },
            context="security_planning pm_decision_requests[1]",
        )


def test_stage_payload_rejects_pm_decision_request_unknown_decision_id() -> None:
    with pytest.raises(RuntimeError, match="references unknown related_decision_ids missing-decision"):
        ArchitectStageOutput.from_payload(
            planning_state="engineering_planning",
            persona_id="architect",
            role_label="Architect",
            payload={
                "findings": ["Invite creation needs a service boundary"],
                "recommendations": ["Create invite service"],
                "required_tasks": ["Build invite service"],
                "child_ticket_specs": [],
                "technical_decisions": [_technical_decision_payload()],
                "pm_decision_requests": [
                    {
                        "request_id": "pm-decision-1",
                        "question": "Should customers see this as an acceptance promise?",
                        "why_it_matters": "The PM owns acceptance interpretation.",
                        "related_decision_ids": ["missing-decision"],
                    }
                ],
                "acceptance_impacts": ["Invite flow works"],
                "mermaid_diagram": "flowchart TD\nA[Start]",
            },
        )


def test_stage_payload_keeps_multiple_pm_decision_requests_for_same_decision_set() -> None:
    payload = ArchitectStageOutput.from_payload(
        planning_state="engineering_planning",
        persona_id="architect",
        role_label="Architect",
        payload={
            "findings": ["Invite creation needs a service boundary"],
            "recommendations": ["Create invite service"],
            "required_tasks": ["Build invite service"],
            "child_ticket_specs": [],
            "technical_decisions": [_technical_decision_payload()],
            "pm_decision_requests": [
                {
                    "request_id": "pm-decision-1",
                    "question": "Should invite links be compliance evidence?",
                    "why_it_matters": "The answer changes acceptance criteria.",
                    "related_decision_ids": ["decision-1"],
                },
                {
                    "request_id": "pm-decision-2",
                    "question": "Should customers see invite links as a compliance promise?",
                    "why_it_matters": "The answer changes release scope.",
                    "related_decision_ids": ["decision-1"],
                },
            ],
            "acceptance_impacts": ["Invite flow works"],
            "mermaid_diagram": "flowchart TD\nA[Start]",
        },
    )

    assert [item.question for item in payload.pm_decision_requests] == [
        "Should invite links be compliance evidence?",
        "Should customers see invite links as a compliance promise?",
    ]


def test_stakeholder_escalation_payload_converts_to_clarification_question() -> None:
    escalation = StakeholderEscalation.from_payload(
        {
            "escalation_id": "stakeholder-1",
            "question": "Does this create a regulated compliance promise?",
            "why_it_matters": "The answer changes customer-facing compliance commitments.",
            "business_impact_area": "risk_compliance",
            "source_pm_decision_request_ids": ["pm-decision-1"],
        },
        context="PM decision resolution stakeholder_escalations[1]",
    )

    question = escalation.to_clarification_question()
    assert question.question == "Does this create a regulated compliance promise?"
    assert question.source_ref == "stakeholder_escalation"


def test_pm_decision_resolution_set_rejects_unknown_request_ids() -> None:
    with pytest.raises(RuntimeError, match="references unknown request_id"):
        PMDecisionResolutionSet.from_payload(
            {
                "resolved_decisions": [],
                "stakeholder_escalations": [
                    {
                        "escalation_id": "stakeholder-1",
                        "question": "Does this create a regulated compliance promise?",
                        "why_it_matters": "The answer changes customer-facing compliance commitments.",
                        "business_impact_area": "risk_compliance",
                        "source_pm_decision_request_ids": ["missing-request"],
                    }
                ],
                "updated_planning_context": {},
            },
            expected_request_ids=("pm-decision-1",),
            context="PM decision resolution payload",
        )


def test_design_planning_payload_rejects_malformed_tool_call() -> None:
    with pytest.raises(RuntimeError, match="Design planning payload has invalid tool_calls item"):
        DesignPlanning.from_payload(
            {
                "selected_plugin_id": "design",
                "decision_state": "pending",
                "stage_artifacts": {},
                "stage_open_questions": [],
                "tool_calls": [{"tool": "stitch.synthesize_screen"}],
                "message": "Need a screen concept",
            }
        )


def test_engineering_seed_plan_payload_rejects_child_missing_done_means() -> None:
    with pytest.raises(RuntimeError, match="Issue seeding engineering_children\\[1\\] missing done_means"):
        EngineeringSeedPlan.from_payload(
            {
                "project_key": "TP",
                "parent_issue": {
                    "summary": "Improve retry reliability",
                    "issue_type": "Story",
                    "objective": "Reduce failed work",
                    "user_value": "Operators see fewer failures",
                    "recommendation": "Ship bounded retries",
                    "scope_in": ["Worker retries"],
                    "scope_out": [],
                    "acceptance_criteria": ["Retries are bounded"],
                    "ui_references": [],
                    "success_outcomes": ["Lower retry failures"],
                    "dependencies": [],
                    "risks": [],
                    "open_questions": [],
                    "labels": [],
                },
                "engineering_children": [
                    {
                        "summary": "Create retry worker",
                        "issue_type": "Sub-task",
                        "capability": "Worker retries",
                        "delivery": "Build bounded worker retries.",
                        "expected_outcome": "Failed jobs retry within limits.",
                        "acceptance_criteria": ["Retries are bounded"],
                        "dependencies": [],
                        "risks": [],
                        "how_to_test": ["Run worker retry integration test"],
                        "labels": [],
                    }
                ],
                "questions": [],
            }
        )


def test_pm_parent_seed_plan_payload_rejects_issue_missing_scope_in() -> None:
    with pytest.raises(RuntimeError, match="PM parent seeding issues\\[1\\] missing scope_in"):
        PmParentSeedPlan.from_payload(
            {
                "project_key": "TP",
                "issues": [
                    {
                        "summary": "Improve retry reliability",
                        "issue_type": "Story",
                        "objective": "Reduce failed work",
                        "user_value": "Operators see fewer failures",
                        "recommendation": "Ship bounded retries",
                        "scope_out": [],
                        "acceptance_criteria": ["Retries are bounded"],
                        "ui_references": [],
                        "success_outcomes": ["Lower retry failures"],
                        "dependencies": [],
                        "risks": [],
                        "open_questions": [],
                        "labels": [],
                    }
                ],
                "questions": [],
            }
        )


def test_pm_interview_plan_payload_rejects_incomplete_without_next_question() -> None:
    with pytest.raises(RuntimeError, match="PM interview payload missing next_question for incomplete brief"):
        PMInterviewPlan.from_payload(
                {
                    "message": "Need more detail.",
                    "brief": _pm_brief_payload(),
                    "status": "question_pending",
                    "missing_slots": ["user_value"],
                    "ready_to_write": False,
                    "evidence": [],
                }
            )


def test_pm_interview_plan_payload_rejects_string_next_question() -> None:
    with pytest.raises(RuntimeError, match="PM interview next_question must be an object"):
        PMInterviewPlan.from_payload(
                {
                    "message": "Need more detail.",
                    "brief": _pm_brief_payload(),
                    "status": "question_pending",
                    "missing_slots": ["user_value"],
                    "ready_to_write": False,
                    "evidence": [],
                    "next_question": "What outcome should this deliver?",
                }
            )


def test_runtime_message_payload_rejects_missing_message() -> None:
    with pytest.raises(RuntimeError, match="Ask answer payload missing message"):
        RuntimeMessage.from_payload({}, context="Ask answer payload")


def test_runtime_message_brief_payload_requires_brief_object() -> None:
    with pytest.raises(RuntimeError, match="Voice room persona payload missing brief"):
        RuntimeMessageBrief.from_payload(
            {"message": "The main risk is attachment churn."},
            context="Voice room persona payload",
        )
