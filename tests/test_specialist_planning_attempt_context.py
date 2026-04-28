from __future__ import annotations

from types import SimpleNamespace

from orchestrator.core.specialist_planning import (
    _PLANNING_STAGES,
    SpecialistPlanningRequest,
    _run_stage,
)


def test_run_stage_preserves_attempt_id_in_invocation_context(monkeypatch) -> None:
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
                "open_behavior_questions": [],
                "acceptance_impacts": [],
            }

    def _fake_create(*, runtime, context, policy_stage, session, settings, issue_key):  # noqa: ANN001, ARG001
        captured["attempt_id"] = context.attempt_id
        captured["attempt"] = context.attempt
        return _FakeStageSession()

    monkeypatch.setattr(
        "orchestrator.core.specialist_planning.RuntimeStageSession.create",
        _fake_create,
    )
    monkeypatch.setattr(
        "orchestrator.core.specialist_planning.render_prompt",
        lambda *_args, **_kwargs: "prompt",
    )

    request = SpecialistPlanningRequest(
        tenant_id="route25",
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

    result = _run_stage(
        session=None,
        settings=None,
        runtime=SimpleNamespace(),
        runtime_for_selector=None,
        request=request,
        stage=_PLANNING_STAGES[0],
    )

    assert captured["attempt_id"] == "attempt-7"
    assert captured["attempt"] == 7
    assert result.blocked is False
