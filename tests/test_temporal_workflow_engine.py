from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

from orchestrator.temporal.payloads import (
    DevelopmentTeamRunActivityResult,
    DevelopmentTeamRunWorkflowInput,
    HandlerWorkflowAdvanceResult,
    HumanInputResumeInput,
)
from orchestrator.temporal.workflow_engine import TemporalWorkflowConfig, TemporalWorkflowEngine
from orchestrator.temporal.workflows.development_team_run import DevelopmentTeamRunWorkflow
from orchestrator.core.workflow_runtime import WorkflowAdvanceRequest


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
        execution_mode="run",
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

    async def _fake_wait_condition(predicate):
        assert predicate() is False
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
    assert resumed_run_id == "run-123"
    assert "timeout" not in resume_captured


def test_temporal_engine_advances_handler_backed_workflow_through_temporal_update(monkeypatch):
    captured: dict[str, object] = {}

    class _FakeHandle:
        async def execute_update(self, update_method, payload):
            captured["update_method"] = update_method
            captured["update_payload"] = payload
            return HandlerWorkflowAdvanceResult(
                handled=True,
                reason=None,
                status="running",
                active_run_id=None,
                last_error=None,
            )

    class _FakeClient:
        async def start_workflow(self, run_method, payload, **kwargs):
            captured["run_method"] = run_method
            captured["run_payload"] = payload
            captured["start_kwargs"] = kwargs
            return _FakeHandle()

    async def _connect(_settings):
        return _FakeClient()

    config = TemporalWorkflowConfig(
        workflow_defn=SimpleNamespace(run="workflow-run", advance="workflow-advance"),
        execution_mode="handler",
        task_queue="custom-queue",
        workflow_execution_timeout_seconds=111,
        workflow_run_timeout_seconds=222,
        activity_start_to_close_timeout_seconds=333,
        human_input_resume_timeout_seconds=444,
    )

    monkeypatch.setattr("orchestrator.temporal.workflow_engine.connect_temporal_client", _connect)
    monkeypatch.setattr(
        "orchestrator.temporal.workflow_engine._temporal_config_for_workflow_type",
        lambda **kwargs: config,
    )

    engine = TemporalWorkflowEngine(
        process_claimed_run_fn=lambda **kwargs: None,
        build_runner_fn=lambda **kwargs: None,
        runtime_kwargs_fn=lambda **kwargs: {},
    )

    workflow_type = SimpleNamespace(
        workflow_type_key="parent_planning",
        handler_key="jira_parent_feature",
        orchestration_backend="temporal",
    )
    request = WorkflowAdvanceRequest(
        workflow_handler_key="jira_parent_feature",
        tenant_id="tenant-a",
        tenant=SimpleNamespace(),
        project_id="project-a",
        issue_key="MAB-215",
        issue_summary="Identity and authorization v1 contract",
        issue_description="description",
        issue_labels=("pm-parent",),
        payload={"request_id": "req-123"},
        webhook_event="jira:issue_updated",
    )

    result = engine.advance_workflow(
        session=SimpleNamespace(),
        settings=SimpleNamespace(),
        session_factory=SimpleNamespace(),
        workflow_type=workflow_type,
        request=request,
        resolve_advance_handler_fn=lambda _handler_key: None,
    )

    assert result.handled is True
    assert captured["run_method"] == "workflow-run"
    assert captured["update_method"] == "workflow-advance"


def test_temporal_registry_includes_parent_planning_and_pr_remediation():
    from orchestrator.temporal.workflow_registry import resolve_temporal_binding_for_handler

    assert resolve_temporal_binding_for_handler(handler_key="jira_parent_feature").handler_key == "jira_parent_feature"
    assert resolve_temporal_binding_for_handler(handler_key="pr_remediation").handler_key == "pr_remediation"
