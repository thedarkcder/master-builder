from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator.core.planning.specialist.models import PLANNING_STAGES, SpecialistPlanningRequest
from orchestrator.core.planning.specialist.stage_runner import SpecialistPlanningStageRunner


def test_run_stage_requires_session_for_attempt_scoped_work_unit(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _FakeStageSession:
        tooling = SimpleNamespace(
            governed_native_prompt_context=lambda: {
                "governed_tools_json": "[]",
                "governed_tool_names": "",
                "governed_tool_instructions": "",
            }
        )

        def invoke_json(self, *, system_prompt: str, user_prompt: str):  # noqa: ARG002
            return {
                "blocked": False,
                "findings": [],
                "recommendations": [],
                "required_tasks": [],
                "technical_decisions": [
                    {
                        "decision_id": "workflow-child-visibility",
                        "area": "architecture",
                        "question": "How should child workflow visibility be represented?",
                        "options": [
                            {
                                "option_id": "operation-state",
                                "title": "Operation state",
                                "description": "Expose visibility from workflow operation state.",
                                "benefits": ["Uses durable workflow state"],
                                "risks": ["Requires operation metadata discipline"],
                                "rejected_reason": "",
                            }
                        ],
                        "selected_option_id": "operation-state",
                        "rationale": "The workflow operation already owns child execution visibility.",
                        "evidence": ["This test exercises workflow operation attempt context"],
                        "confidence": "high",
                        "product_impact": "none",
                    }
                ],
                "pm_decision_requests": [],
                "child_ticket_specs": [
                    {
                        "summary": "Build child workflow",
                        "capability": "workflow",
                        "delivery": "implementation",
                        "expected_outcome": "Child work can be executed",
                        "acceptance_criteria": ["Child work is visible"],
                        "how_to_test": ["Run the workflow"],
                        "done_means": ["Workflow is complete"],
                        "dependencies": [],
                        "risks": [],
                        "labels": [],
                    }
                ],
                "acceptance_impacts": [],
            }

    def _fake_create(*, runtime, context, policy_stage, session, settings, issue_key):  # noqa: ANN001, ARG001
        captured["attempt_id"] = context.attempt_id
        captured["attempt"] = context.attempt
        return _FakeStageSession()

    monkeypatch.setattr(
        "orchestrator.core.planning.specialist.stage_runner.RuntimeStageSession.create",
        _fake_create,
    )
    monkeypatch.setattr(
        "orchestrator.core.planning.specialist.stage_runner.render_prompt",
        lambda *_args, **_kwargs: "prompt",
    )

    request = SpecialistPlanningRequest(
        tenant_id="example",
        project_id="project-1",
        parent_issue_key="MAB-215",
        parent_summary="Parent summary",
        parent_description="Parent description",
        product_brief={"objective": "Ship it"},
        workflow_id="parent_planning:MAB-215",
        operation_id="op-1",
        attempt_id="attempt-7",
        attempt=7,
    )

    with pytest.raises(RuntimeError, match="require a database session"):
        SpecialistPlanningStageRunner().run_stage(
            session=None,
            settings=None,
            runtime=SimpleNamespace(),
            runtime_for_selector=None,
            request=request,
            stage=PLANNING_STAGES[0],
        )

    assert captured["attempt_id"] == "attempt-7"
    assert captured["attempt"] == 7
