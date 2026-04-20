from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

from orchestrator.temporal.payloads import DevelopmentTeamRunActivityResult, DevelopmentTeamRunWorkflowInput, HumanInputResumeInput
from orchestrator.temporal.workflow_engine import TemporalWorkflowConfig, TemporalWorkflowEngine
from orchestrator.temporal.workflows.development_team_run import DevelopmentTeamRunWorkflow


def _workflow_input(**overrides) -> DevelopmentTeamRunWorkflowInput:
    payload = {
        "workflow_id": "workflow-123",
        "run_id": "run-123",
        "claim_id": "claim-123",
        "tenant_id": "tenant-a",
        "project_id": "project-a",
        "issue_key": "MAB-215",
        "workflow_execution_timeout_seconds": 86400,
        "workflow_run_timeout_seconds": 43200,
        "activity_start_to_close_timeout_seconds": 321,
        "human_input_resume_timeout_seconds": 654,
    }
    payload.update(overrides)
    return DevelopmentTeamRunWorkflowInput(**payload)


def test_temporal_engine_uses_workflow_type_timeouts(monkeypatch):
    captured: dict[str, object] = {}

    class _FakeClient:
        async def start_workflow(self, run_method, payload, **kwargs):
            captured["run_method"] = run_method
            captured["payload"] = payload
            captured["kwargs"] = kwargs

    async def _connect(_settings):
        return _FakeClient()

    config = TemporalWorkflowConfig(
        workflow_defn=SimpleNamespace(run="workflow-run"),
        task_queue="custom-queue",
        workflow_execution_timeout_seconds=111,
        workflow_run_timeout_seconds=222,
        activity_start_to_close_timeout_seconds=333,
        human_input_resume_timeout_seconds=444,
    )

    monkeypatch.setattr("orchestrator.temporal.workflow_engine.connect_temporal_client", _connect)
    monkeypatch.setattr(
        "orchestrator.temporal.workflow_engine._temporal_config_for_workflow",
        lambda **kwargs: config,
    )

    engine = TemporalWorkflowEngine(
        process_claimed_run_fn=lambda **kwargs: None,
        build_runner_fn=lambda **kwargs: None,
        runtime_kwargs_fn=lambda **kwargs: {},
    )

    run = SimpleNamespace(
        run_id="run-123",
        tenant_id="tenant-a",
        project_id="project-a",
        issue_key="MAB-215",
    )
    workflow = SimpleNamespace(workflow_id="workflow-123")

    returned = engine.start_workflow(
        session=SimpleNamespace(),
        settings=SimpleNamespace(),
        session_factory=SimpleNamespace(),
        workflow=workflow,
        run=run,
        claim_id="claim-123",
    )

    assert returned is run
    assert captured["run_method"] == "workflow-run"
    payload = captured["payload"]
    assert payload.workflow_execution_timeout_seconds == 111
    assert payload.workflow_run_timeout_seconds == 222
    assert payload.activity_start_to_close_timeout_seconds == 333
    assert payload.human_input_resume_timeout_seconds == 444
    assert captured["kwargs"]["task_queue"] == "custom-queue"
    assert captured["kwargs"]["execution_timeout"] == timedelta(seconds=111)
    assert captured["kwargs"]["run_timeout"] == timedelta(seconds=222)


def test_development_team_run_workflow_uses_configured_activity_timeouts(monkeypatch):
    initial_captured: dict[str, object] = {}
    resume_captured: dict[str, object] = {}
    workflow_defn = DevelopmentTeamRunWorkflow()

    async def _fake_execute_activity(fn, payload, *, start_to_close_timeout):
        target_name = getattr(fn, "__name__", "")
        if target_name == "execute_claimed_run_activity":
            initial_captured["payload"] = payload
            initial_captured["timeout"] = start_to_close_timeout
            return DevelopmentTeamRunActivityResult(
                workflow_id="workflow-123",
                run_id="run-123",
                status="waiting_for_input",
                issue_key="MAB-215",
                pending_request_id="request-123",
            )
        resume_captured["payload"] = payload
        resume_captured["timeout"] = start_to_close_timeout
        return DevelopmentTeamRunActivityResult(
            workflow_id="workflow-123",
            run_id="run-456",
            status="running",
            issue_key="MAB-215",
        )

    async def _fake_wait_condition(predicate, *, timeout):
        assert predicate() is False
        assert timeout == timedelta(seconds=654)
        workflow_defn._status = "completed"

    monkeypatch.setattr(
        "orchestrator.temporal.workflows.development_team_run.workflow.execute_activity",
        _fake_execute_activity,
    )
    monkeypatch.setattr(
        "orchestrator.temporal.workflows.development_team_run.workflow.wait_condition",
        _fake_wait_condition,
    )

    payload = _workflow_input()
    state = asyncio.run(workflow_defn.run(payload))

    assert state.status == "completed"
    assert initial_captured["payload"] == payload
    assert initial_captured["timeout"] == timedelta(seconds=321)

    resumed_run_id = asyncio.run(workflow_defn.resume_human_input(HumanInputResumeInput(request_id="request-123")))
    assert resumed_run_id == "run-456"
    assert resume_captured["timeout"] == timedelta(seconds=654)
