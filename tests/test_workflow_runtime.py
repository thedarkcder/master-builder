from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import sentinel

from orchestrator.core.workflow_engine import WorkflowEngineState
from orchestrator.core.workflow_runtime import WorkflowAdvanceRequest, build_workflow_runtime


class FakeEngine:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def start_workflow(self, **kwargs):
        self.calls.append(("start", kwargs))
        return sentinel.started_run

    def resume_workflow(self, **kwargs):
        self.calls.append(("resume", kwargs))
        return sentinel.resumed_run

    def query_workflow(self, **kwargs):
        self.calls.append(("query", kwargs))
        return WorkflowEngineState(
            workflow_id="wf-123",
            backend="temporal",
            status="running",
            active_run_id="run-123",
        )

    def retry_workflow_operation(self, **kwargs):
        self.calls.append(("retry", kwargs))
        return sentinel.retry_handle


def test_workflow_runtime_delegates_start_resume_query_and_retry(monkeypatch):
    engine = FakeEngine()
    session_factory = sentinel.session_factory

    monkeypatch.setattr(
        "orchestrator.core.workflow_runtime.build_workflow_engine",
        lambda **kwargs: engine,
    )
    monkeypatch.setattr(
        "orchestrator.core.workflow_runtime.create_session_factory_for_engine",
        lambda **kwargs: session_factory,
    )

    session = sentinel.session
    settings = sentinel.settings
    workflow = SimpleNamespace(workflow_id="wf-123")
    run = sentinel.run
    request = sentinel.request
    operation = sentinel.operation

    runtime = build_workflow_runtime(
        session=session,
        settings=settings,
        process_claimed_run_fn=sentinel.process_claimed_run_fn,
        build_runner_fn=sentinel.build_runner_fn,
        runtime_kwargs_fn=sentinel.runtime_kwargs_fn,
        retry_workflow_operation_fn=sentinel.retry_workflow_operation_fn,
    )

    assert runtime.start_execution(workflow=workflow, run=run, claim_id="claim-123") is sentinel.started_run
    assert runtime.resume_input(workflow=workflow, request=request) is sentinel.resumed_run
    state = runtime.query_execution(workflow=workflow)
    assert state.workflow_id == "wf-123"
    assert runtime.retry_operation(workflow=workflow, operation=operation) is sentinel.retry_handle

    assert [name for name, _ in engine.calls] == ["start", "resume", "query", "retry"]
    for _, kwargs in engine.calls:
        if "settings" in kwargs:
            assert kwargs["settings"] is settings
        if "session" in kwargs:
            assert kwargs["session"] is session
        if "session_factory" in kwargs:
            assert kwargs["session_factory"] is session_factory


def test_workflow_runtime_delegates_advance_to_handler_resolved_from_workflow_type(monkeypatch):
    session = sentinel.session
    settings = sentinel.settings
    workflow_type = SimpleNamespace(handler_key="jira_parent_feature")
    request = WorkflowAdvanceRequest(
        workflow_handler_key="jira_parent_feature",
        tenant_id="tenant-a",
        tenant=sentinel.tenant,
        project_id="project-a",
        issue_key="MAB-215",
        issue_labels=("pm-parent",),
        payload={"request_id": "req-1"},
        webhook_event="issue_updated",
    )
    calls: list[tuple[str, object]] = []

    class _Handler:
        def advance(self, **kwargs):
            calls.append(("advance", kwargs))
            return sentinel.advance_result

    monkeypatch.setattr(
        "orchestrator.core.workflow_runtime.get_workflow_type_by_handler_key",
        lambda *args, **kwargs: workflow_type,
    )

    runtime = build_workflow_runtime(
        session=session,
        settings=settings,
        process_claimed_run_fn=sentinel.process_claimed_run_fn,
        build_runner_fn=sentinel.build_runner_fn,
        runtime_kwargs_fn=sentinel.runtime_kwargs_fn,
        resolve_advance_handler_fn=lambda handler_key: (_Handler() if handler_key == "jira_parent_feature" else None),
    )

    assert runtime.advance(request=request) is sentinel.advance_result
    assert len(calls) == 1
    assert calls[0][0] == "advance"
    kwargs = calls[0][1]
    assert kwargs["session"] is session
    assert kwargs["settings"] is settings
    assert kwargs["workflow_type"] is workflow_type
    assert kwargs["request"] is request
    lifecycle = kwargs["lifecycle"]
    assert hasattr(lifecycle, "ensure_issue_execution")
    assert hasattr(lifecycle, "mark_operation_completed")
