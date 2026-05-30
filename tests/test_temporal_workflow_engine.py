from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest

from temporalio.exceptions import ApplicationError

from orchestrator.core.planning.specialist import RetryableSpecialistPlanningContractError
from orchestrator.core.workflow.advance import WorkflowAdvanceOutcome
from orchestrator.core.workflow.execution_projection import WorkflowExecutionReference, WorkflowSourceReference
from orchestrator.core.workflow.operation_service import WorkflowOperationAttemptAlreadyRunningError
from orchestrator.core.workflow.runtime import WorkflowAdvanceRequest, WorkflowTrigger
from orchestrator.temporal.payloads import (
    DevelopmentTeamRunActivityResult,
    DevelopmentTeamRunWorkflowInput,
    HandlerWorkflowAdvanceInput,
    HandlerWorkflowAdvanceResult,
    HumanInputResumeInput,
    ProjectDeploymentSetupWorkflowInput,
    WorkflowOperationRetryInput,
    WorkflowOperationRetryResult,
)
from orchestrator.temporal.workflow_engine import (
    TemporalWorkflowConfig,
    TemporalWorkflowEngine,
    notify_temporal_run_result,
)
from orchestrator.temporal.workflows.development_team_run import DevelopmentTeamRunWorkflow
from orchestrator.temporal.workflows.handler_backed_workflow import HandlerBackedWorkflow
from orchestrator.temporal.workflows.project_deployment_setup import ProjectDeploymentSetupWorkflow


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
        retry_max_attempts=4,
        retry_initial_interval_seconds=5,
        retry_max_interval_seconds=30,
        retry_backoff_coefficient=2.0,
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
        workflow_handler_registry=None,
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


def test_development_team_run_workflow_waits_for_execution_worker_result_and_uses_resume_timeout(monkeypatch):
    resume_captured: dict[str, object] = {}
    workflow_defn = DevelopmentTeamRunWorkflow()
    wait_calls = 0

    async def _fake_execute_activity(fn, payload, *, start_to_close_timeout):
        target_name = getattr(fn, "__name__", "")
        assert target_name == "resume_human_input_activity"
        resume_captured["payload"] = payload
        resume_captured["timeout"] = start_to_close_timeout
        return DevelopmentTeamRunActivityResult(
            workflow_id="workflow-123",
            run_id="run-456",
            status="running",
            issue_key="MAB-215",
        )

    async def _fake_wait_condition(predicate):
        nonlocal wait_calls
        wait_calls += 1
        assert predicate() is False
        if wait_calls == 1:
            workflow_defn._apply_result(
                DevelopmentTeamRunActivityResult(
                    workflow_id="workflow-123",
                    run_id="run-123",
                    status="waiting_for_input",
                    issue_key="MAB-215",
                    pending_request_id="request-123",
                )
            )
            return
        workflow_defn._apply_result(
            DevelopmentTeamRunActivityResult(
                workflow_id="workflow-123",
                run_id="run-123",
                status="succeeded",
                issue_key="MAB-215",
            )
        )

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

    assert state.status == "succeeded"
    assert wait_calls == 2
    assert workflow_defn._activity_timeout_seconds == 321

    workflow_defn._apply_result(
        DevelopmentTeamRunActivityResult(
            workflow_id="workflow-123",
            run_id="run-123",
            status="waiting_for_input",
            issue_key="MAB-215",
            pending_request_id="request-123",
        )
    )
    resumed_run_id = asyncio.run(workflow_defn.resume_human_input(HumanInputResumeInput(request_id="request-123")))
    assert resumed_run_id == "run-456"
    assert resume_captured["payload"] == HumanInputResumeInput(request_id="request-123")
    assert resume_captured["timeout"] == timedelta(seconds=654)


def test_development_team_run_workflow_records_execution_worker_result_update(monkeypatch):
    workflow_defn = DevelopmentTeamRunWorkflow()

    async def _fake_wait_condition(predicate):
        assert predicate() is False
        workflow_defn._apply_result(
            DevelopmentTeamRunActivityResult(
                workflow_id="workflow-123",
                run_id="run-123",
                status="succeeded",
                issue_key="MAB-215",
            )
        )

    monkeypatch.setattr("orchestrator.temporal.workflows.development_team_run.workflow.wait_condition", _fake_wait_condition)

    payload = _workflow_input()
    state = asyncio.run(workflow_defn.run(payload))

    assert state.status == "succeeded"
    updated_run_id = asyncio.run(
        workflow_defn.record_run_result(
            DevelopmentTeamRunActivityResult(
                workflow_id="workflow-123",
                run_id="run-789",
                status="failed",
                issue_key="MAB-215",
                last_error="failed after external execution",
            )
        )
    )
    assert updated_run_id == "run-789"
    assert workflow_defn.describe_state().status == "failed"


def test_development_team_run_workflow_rejects_mismatched_pending_request_id():
    workflow_defn = DevelopmentTeamRunWorkflow()
    workflow_defn._apply_result(
        DevelopmentTeamRunActivityResult(
            workflow_id="workflow-123",
            run_id="run-123",
            status="waiting_for_input",
            issue_key="MAB-215",
            pending_request_id="request-123",
        )
    )

    with pytest.raises(ValueError, match="request-123"):
        asyncio.run(workflow_defn.resume_human_input(HumanInputResumeInput(request_id="request-456")))


def test_notify_temporal_run_result_includes_pending_request_id(monkeypatch):
    captured: dict[str, object] = {}

    class _FakeResult:
        def scalar_one_or_none(self):
            return "request-123"

    class _FakeSession:
        def execute(self, _query):
            return _FakeResult()

    class _FakeHandle:
        async def execute_update(self, update_method, payload):
            captured["update_method"] = update_method
            captured["payload"] = payload

    class _FakeClient:
        def get_workflow_handle_for(self, workflow_defn, workflow_id):
            captured["workflow_defn"] = workflow_defn
            captured["workflow_id"] = workflow_id
            return _FakeHandle()

    async def _connect(_settings):
        return _FakeClient()

    workflow_defn = SimpleNamespace(record_run_result="record-result-update")
    config = TemporalWorkflowConfig(
        workflow_defn=workflow_defn,
        execution_mode="run",
        task_queue="custom-queue",
        workflow_execution_timeout_seconds=111,
        workflow_run_timeout_seconds=222,
        activity_start_to_close_timeout_seconds=333,
        human_input_resume_timeout_seconds=444,
        retry_max_attempts=4,
        retry_initial_interval_seconds=5,
        retry_max_interval_seconds=30,
        retry_backoff_coefficient=2.0,
    )

    monkeypatch.setattr("orchestrator.temporal.workflow_engine.connect_temporal_client", _connect)
    monkeypatch.setattr(
        "orchestrator.temporal.workflow_engine._temporal_config_for_workflow",
        lambda **_kwargs: config,
    )

    notify_temporal_run_result(
        session=_FakeSession(),  # type: ignore[arg-type]
        settings=SimpleNamespace(),
        workflow=SimpleNamespace(workflow_id="workflow-123"),
        run=SimpleNamespace(
            run_id="run-123",
            status="waiting_for_input",
            issue_key="MAB-215",
            claim_id="claim-123",
            last_error=None,
        ),
    )

    payload = captured["payload"]
    assert isinstance(payload, DevelopmentTeamRunActivityResult)
    assert captured["workflow_id"] == "workflow:workflow-123"
    assert captured["update_method"] == "record-result-update"
    assert payload.pending_request_id == "request-123"


def test_temporal_engine_advances_handler_backed_workflow_through_temporal_update(monkeypatch):
    captured: dict[str, object] = {}

    class _FakeClient:
        async def execute_update_with_start_workflow(self, update_method, payload, *, start_workflow_operation):
            captured["update_method"] = update_method
            captured["update_payload"] = payload
            captured["start_operation"] = start_workflow_operation
            return HandlerWorkflowAdvanceResult(
                handled=True,
                reason=None,
                status="running",
                active_run_id=None,
                last_error=None,
            )

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
        retry_max_attempts=4,
        retry_initial_interval_seconds=5,
        retry_max_interval_seconds=30,
        retry_backoff_coefficient=2.0,
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
        workflow_handler_registry=None,
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
        execution=WorkflowExecutionReference(
            key="MAB-215",
            source=WorkflowSourceReference(
                source_system="jira",
                source_ref="MAB-215",
                display_name="Identity and authorization v1 contract",
                description="description",
                attributes={"jira_issue_labels": ["pm-parent"]},
            ),
        ),
        payload={"request_id": "req-123"},
        trigger=WorkflowTrigger(event="jira:issue_updated"),
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
    assert captured["update_method"] == "workflow-advance"
    assert captured["update_payload"].workflow_id == "parent_planning:MAB-215"
    assert captured["update_payload"].retry_max_attempts == 4
    assert captured["update_payload"].retry_initial_interval_seconds == 5
    assert captured["update_payload"].retry_max_interval_seconds == 30
    assert captured["update_payload"].retry_backoff_coefficient == 2.0
    start_operation = captured["start_operation"]
    start_input = start_operation._start_workflow_input
    assert start_input.workflow == "workflow-run"
    assert start_input.id == "workflow:parent_planning:MAB-215"
    assert start_input.task_queue == "custom-queue"
    assert start_input.id_conflict_policy.name == "USE_EXISTING"
    assert start_input.args[0].workflow_id == "parent_planning:MAB-215"
    assert start_input.args[0].workflow_handler_key == "jira_parent_feature"


def test_temporal_engine_handler_retry_waits_for_retry_activity_result(monkeypatch):
    captured: dict[str, object] = {}

    class _FakeHandle:
        async def execute_update(self, update_method, payload):
            captured["update_method"] = update_method
            captured["update_payload"] = payload
            return WorkflowOperationRetryResult(
                operation_id="operation-backlog-planning",
                workflow_id="parent_planning:MAB-233",
                operation_type="backlog_planning",
                operation_status="running",
                workflow_status="running",
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
        workflow_defn=SimpleNamespace(run="workflow-run", retry_operation="workflow-retry"),
        execution_mode="handler",
        task_queue="custom-queue",
        workflow_execution_timeout_seconds=111,
        workflow_run_timeout_seconds=222,
        activity_start_to_close_timeout_seconds=333,
        human_input_resume_timeout_seconds=444,
        retry_max_attempts=4,
        retry_initial_interval_seconds=5,
        retry_max_interval_seconds=30,
        retry_backoff_coefficient=2.0,
    )

    monkeypatch.setattr("orchestrator.temporal.workflow_engine.connect_temporal_client", _connect)
    monkeypatch.setattr(
        "orchestrator.temporal.workflow_engine._temporal_config_for_workflow",
        lambda **kwargs: config,
    )
    monkeypatch.setattr(
        "orchestrator.temporal.workflow_engine.get_workflow_type",
        lambda session, workflow_type_key: SimpleNamespace(handler_key="jira_parent_feature"),
    )

    engine = TemporalWorkflowEngine(
        process_claimed_run_fn=lambda **kwargs: None,
        build_runner_fn=lambda **kwargs: None,
        runtime_kwargs_fn=lambda **kwargs: {},
        workflow_handler_registry=None,
    )
    workflow = SimpleNamespace(
        workflow_id="parent_planning:MAB-233",
        workflow_type_key="parent_planning",
    )
    operation = SimpleNamespace(
        operation_id="operation-backlog-planning",
        operation_type="backlog_planning",
        status="failed",
    )

    handle = engine.retry_workflow_operation(
        session=SimpleNamespace(),
        settings=SimpleNamespace(),
        session_factory=SimpleNamespace(),
        workflow=workflow,
        operation=operation,
    )

    assert handle.operation_id == operation.operation_id
    assert handle.workflow_id == workflow.workflow_id
    assert handle.operation_type == operation.operation_type
    assert handle.status == "running"
    assert captured["update_method"] == "workflow-retry"
    payload = captured["update_payload"]
    assert payload.workflow_id == workflow.workflow_id
    assert payload.operation_id == operation.operation_id
    assert payload.retry_max_attempts == 4
    assert payload.retry_initial_interval_seconds == 5
    assert payload.retry_max_interval_seconds == 30
    assert payload.retry_backoff_coefficient == 2.0


def test_handler_backed_workflow_advances_with_single_activity_input(monkeypatch):
    captured: dict[str, object] = {}
    workflow_defn = HandlerBackedWorkflow()
    workflow_defn._workflow_id = "workflow-123"
    workflow_defn._activity_timeout_seconds = 321

    async def _fake_execute_activity(fn, payload, *, start_to_close_timeout, retry_policy=None):
        captured["fn"] = fn
        captured["payload"] = payload
        captured["timeout"] = start_to_close_timeout
        captured["retry_policy"] = retry_policy
        return HandlerWorkflowAdvanceResult(
            handled=True,
            reason=None,
            status="running",
            active_run_id="run-123",
            last_error=None,
        )

    monkeypatch.setattr(
        "orchestrator.temporal.workflows.handler_backed_workflow.workflow.execute_activity",
        _fake_execute_activity,
    )

    payload = HandlerWorkflowAdvanceInput(
        workflow_id="parent_planning:MAB-229",
        workflow_handler_key="jira_parent_feature",
        tenant_id="tenant-a",
        project_id="project-a",
        execution_key="MAB-229",
        source_system="jira",
        source_ref="MAB-229",
        source_display_name="WorkOS identity redesign",
        source_description="description",
        source_attributes={"jira_issue_labels": ["pm-parent"]},
        payload={"request_id": "req-123"},
        trigger_event="issue_created",
    )

    result = asyncio.run(workflow_defn.advance(payload))

    assert result.handled is True
    activity_payload = captured["payload"]
    assert activity_payload == payload
    assert captured["timeout"] == timedelta(seconds=321)
    assert captured["retry_policy"] is None


def test_handler_backed_workflow_advance_does_not_depend_on_initialized_workflow_state(monkeypatch):
    captured: dict[str, object] = {}
    workflow_defn = HandlerBackedWorkflow()
    workflow_defn._activity_timeout_seconds = 321

    async def _fake_execute_activity(fn, payload, *, start_to_close_timeout, retry_policy=None):
        captured["fn"] = fn
        captured["payload"] = payload
        captured["timeout"] = start_to_close_timeout
        captured["retry_policy"] = retry_policy
        return HandlerWorkflowAdvanceResult(
            handled=True,
            reason=None,
            status="running",
            active_run_id="run-123",
            last_error=None,
        )

    monkeypatch.setattr(
        "orchestrator.temporal.workflows.handler_backed_workflow.workflow.execute_activity",
        _fake_execute_activity,
    )

    payload = HandlerWorkflowAdvanceInput(
        workflow_id="parent_planning:MAB-230",
        workflow_handler_key="jira_parent_feature",
        tenant_id="tenant-a",
        project_id="project-a",
        execution_key="MAB-230",
        source_system="jira",
        source_ref="MAB-230",
        source_display_name="WorkOS identity redesign",
        source_description="description",
        source_attributes={"jira_issue_labels": ["pm-parent"]},
        payload={"request_id": "req-123"},
        trigger_event="issue_created",
    )

    result = asyncio.run(workflow_defn.advance(payload))

    assert result.handled is True
    activity_payload = captured["payload"]
    assert activity_payload == payload
    assert captured["timeout"] == timedelta(seconds=321)
    assert captured["retry_policy"] is None


def test_handler_backed_workflow_advance_uses_retry_policy_from_payload(monkeypatch):
    captured: dict[str, object] = {}
    workflow_defn = HandlerBackedWorkflow()
    workflow_defn._activity_timeout_seconds = 321

    async def _fake_execute_activity(fn, payload, *, start_to_close_timeout, retry_policy=None):
        captured["fn"] = fn
        captured["payload"] = payload
        captured["timeout"] = start_to_close_timeout
        captured["retry_policy"] = retry_policy
        return HandlerWorkflowAdvanceResult(
            handled=True,
            reason=None,
            status="running",
            active_run_id="run-123",
            last_error=None,
        )

    monkeypatch.setattr(
        "orchestrator.temporal.workflows.handler_backed_workflow.workflow.execute_activity",
        _fake_execute_activity,
    )

    payload = HandlerWorkflowAdvanceInput(
        workflow_id="parent_planning:MAB-232",
        workflow_handler_key="jira_parent_feature",
        tenant_id="tenant-a",
        project_id="project-a",
        execution_key="MAB-232",
        source_system="jira",
        source_ref="MAB-232",
        source_display_name="WorkOS identity redesign",
        source_description="description",
        source_attributes={"jira_issue_labels": ["pm-parent"]},
        payload={"request_id": "req-123"},
        trigger_event="issue_created",
        retry_max_attempts=4,
        retry_initial_interval_seconds=5,
        retry_max_interval_seconds=30,
        retry_backoff_coefficient=2.0,
    )

    result = asyncio.run(workflow_defn.advance(payload))

    assert result.handled is True
    retry_policy = captured["retry_policy"]
    assert retry_policy is not None
    assert retry_policy.maximum_attempts == 4
    assert retry_policy.initial_interval == timedelta(seconds=5)
    assert retry_policy.maximum_interval == timedelta(seconds=30)
    assert retry_policy.backoff_coefficient == 2.0
    assert retry_policy.non_retryable_error_types == ("terminal_workflow_advance_error",)


def test_handler_backed_workflow_operation_retry_uses_retry_policy_from_payload(monkeypatch):
    captured: dict[str, object] = {}
    workflow_defn = HandlerBackedWorkflow()
    workflow_defn._activity_timeout_seconds = 321

    async def _fake_execute_activity(fn, payload, *, start_to_close_timeout, retry_policy=None):  # noqa: ANN001
        captured["fn"] = fn
        captured["payload"] = payload
        captured["timeout"] = start_to_close_timeout
        captured["retry_policy"] = retry_policy
        return WorkflowOperationRetryResult(
            operation_id="operation-backlog-planning",
            workflow_id="parent_planning:MAB-233",
            operation_type="backlog_planning",
            operation_status="running",
            workflow_status="running",
        )

    monkeypatch.setattr(
        "orchestrator.temporal.workflows.handler_backed_workflow.workflow.execute_activity",
        _fake_execute_activity,
    )

    payload = WorkflowOperationRetryInput(
        workflow_id="parent_planning:MAB-233",
        operation_id="operation-backlog-planning",
        retry_max_attempts=4,
        retry_initial_interval_seconds=5,
        retry_max_interval_seconds=30,
        retry_backoff_coefficient=2.0,
    )

    result = asyncio.run(workflow_defn.retry_operation(payload))

    assert result.operation_status == "running"
    retry_policy = captured["retry_policy"]
    assert retry_policy is not None
    assert retry_policy.maximum_attempts == 4
    assert retry_policy.initial_interval == timedelta(seconds=5)
    assert retry_policy.maximum_interval == timedelta(seconds=30)
    assert retry_policy.backoff_coefficient == 2.0
    assert retry_policy.non_retryable_error_types == ("terminal_workflow_operation_retry_error",)


def test_process_handler_workflow_advance_activity_raises_retryable_application_error(monkeypatch):
    monkeypatch.setattr(
        "orchestrator.temporal.activities.handler_workflow.get_settings",
        lambda: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "orchestrator.temporal.activities.handler_workflow.create_session_factory",
        lambda: lambda: _FakeSessionContextManager(
            session=_FakeSession(
                tenant=SimpleNamespace(tenant_id="tenant-a"),
                workflow_type=SimpleNamespace(handler_key="jira_parent_feature"),
            )
        ),
    )
    monkeypatch.setattr(
        "orchestrator.temporal.activities.handler_workflow.get_workflow_type_by_handler_key",
        lambda session, handler_key: session.workflow_type,
    )
    monkeypatch.setattr(
        "orchestrator.temporal.activities.handler_workflow.execute_workflow_advance",
        lambda **kwargs: (_ for _ in ()).throw(
            RetryableSpecialistPlanningContractError(
                "Codex returned engineering_planning child_ticket_specs[1] without done_means"
            )
        ),
    )

    payload = HandlerWorkflowAdvanceInput(
        workflow_id="parent_planning:MAB-232",
        workflow_handler_key="jira_parent_feature",
        tenant_id="tenant-a",
        project_id="project-a",
        execution_key="MAB-232",
        source_system="jira",
        source_ref="MAB-232",
    )

    with pytest.raises(ApplicationError) as exc_info:
        from orchestrator.temporal.activities.handler_workflow import process_handler_workflow_advance_activity

        process_handler_workflow_advance_activity(payload)

    assert exc_info.value.type == "retryable_invalid_model_output"
    assert exc_info.value.non_retryable is False


def test_process_handler_workflow_advance_activity_raises_terminal_application_error(monkeypatch):
    monkeypatch.setattr(
        "orchestrator.temporal.activities.handler_workflow.get_settings",
        lambda: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "orchestrator.temporal.activities.handler_workflow.create_session_factory",
        lambda: lambda: _FakeSessionContextManager(
            session=_FakeSession(
                tenant=SimpleNamespace(tenant_id="tenant-a"),
                workflow_type=SimpleNamespace(handler_key="jira_parent_feature"),
            )
        ),
    )
    monkeypatch.setattr(
        "orchestrator.temporal.activities.handler_workflow.get_workflow_type_by_handler_key",
        lambda session, handler_key: session.workflow_type,
    )
    monkeypatch.setattr(
        "orchestrator.temporal.activities.handler_workflow.execute_workflow_advance",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    payload = HandlerWorkflowAdvanceInput(
        workflow_id="parent_planning:MAB-232",
        workflow_handler_key="jira_parent_feature",
        tenant_id="tenant-a",
        project_id="project-a",
        execution_key="MAB-232",
        source_system="jira",
        source_ref="MAB-232",
    )

    with pytest.raises(ApplicationError) as exc_info:
        from orchestrator.temporal.activities.handler_workflow import process_handler_workflow_advance_activity

        process_handler_workflow_advance_activity(payload)

    assert exc_info.value.type == "terminal_workflow_advance_error"
    assert exc_info.value.non_retryable is True


def test_process_handler_workflow_advance_activity_returns_current_state_for_active_operation(monkeypatch):
    workflow = SimpleNamespace(
        workflow_id="parent_planning:MAB-232",
        status="waiting_for_input",
        active_run_id=None,
        last_error=None,
    )
    fake_session = _FakeSession(
        tenant=SimpleNamespace(tenant_id="tenant-a"),
        workflow_type=SimpleNamespace(handler_key="jira_parent_feature"),
        workflow=workflow,
    )
    monkeypatch.setattr(
        "orchestrator.temporal.activities.handler_workflow.get_settings",
        lambda: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "orchestrator.temporal.activities.handler_workflow.create_session_factory",
        lambda: lambda: _FakeSessionContextManager(session=fake_session),
    )
    monkeypatch.setattr(
        "orchestrator.temporal.activities.handler_workflow.get_workflow_type_by_handler_key",
        lambda session, handler_key: session.workflow_type,
    )
    monkeypatch.setattr(
        "orchestrator.temporal.activities.handler_workflow.execute_workflow_advance",
        lambda **kwargs: (_ for _ in ()).throw(
            WorkflowOperationAttemptAlreadyRunningError(
                "Workflow operation brief_normalization already has active attempt 1 (attempt-running)."
            )
        ),
    )

    payload = HandlerWorkflowAdvanceInput(
        workflow_id=workflow.workflow_id,
        workflow_handler_key="jira_parent_feature",
        tenant_id="tenant-a",
        project_id="project-a",
        execution_key="MAB-232",
        source_system="jira",
        source_ref="MAB-232",
    )

    from orchestrator.temporal.activities.handler_workflow import process_handler_workflow_advance_activity

    result = process_handler_workflow_advance_activity(payload)

    assert fake_session.rollback_called is True
    assert result.handled is True
    assert result.reason == "workflow_operation_already_active"
    assert result.status == "waiting_for_input"
    assert result.active_run_id is None
    assert result.last_error is None


def test_process_handler_workflow_advance_activity_allows_explicit_no_persist_noop(monkeypatch):
    monkeypatch.setattr(
        "orchestrator.temporal.activities.handler_workflow.get_settings",
        lambda: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "orchestrator.temporal.activities.handler_workflow.create_session_factory",
        lambda: lambda: _FakeSessionContextManager(
            session=_FakeSession(
                tenant=SimpleNamespace(tenant_id="tenant-a"),
                workflow_type=SimpleNamespace(handler_key="jira_parent_feature"),
            )
        ),
    )
    monkeypatch.setattr(
        "orchestrator.temporal.activities.handler_workflow.get_workflow_type_by_handler_key",
        lambda session, handler_key: session.workflow_type,
    )
    monkeypatch.setattr(
        "orchestrator.temporal.activities.handler_workflow.execute_workflow_advance",
        lambda **kwargs: WorkflowAdvanceOutcome(
            handled=True,
            reason="pm_parent_non_material_change",
            requires_persisted_execution=False,
        ),
    )

    payload = HandlerWorkflowAdvanceInput(
        workflow_id="parent_planning:MAB-232",
        workflow_handler_key="jira_parent_feature",
        tenant_id="tenant-a",
        project_id="project-a",
        execution_key="MAB-232",
        source_system="jira",
        source_ref="MAB-232",
    )

    from orchestrator.temporal.activities.handler_workflow import process_handler_workflow_advance_activity

    result = process_handler_workflow_advance_activity(payload)

    assert result.handled is True
    assert result.reason == "pm_parent_non_material_change"
    assert result.status == "ignored"


def test_retry_handler_workflow_operation_activity_dispatches_projectless_workflow(monkeypatch):
    workflow = SimpleNamespace(
        workflow_id="tenant_workflow:abc",
        workflow_type_key="tenant_workflow",
        tenant_id="tenant-a",
        project_id=None,
        status="failed",
        active_run_id=None,
        last_error="operation failed",
    )
    operation = SimpleNamespace(
        operation_id="op-123",
        workflow_id="tenant_workflow:abc",
        operation_type="tenant_operation",
    )
    registry = SimpleNamespace(resolve_operation_retry_handler=lambda _handler_key: None)
    captured: dict[str, object] = {}

    class _ProjectlessRetrySession:
        def get(self, model, key):
            model_name = getattr(model, "__name__", "")
            if model_name == "WorkflowExecution":
                return workflow if key == workflow.workflow_id else None
            if model_name == "WorkflowOperation":
                return operation if key == operation.operation_id else None
            raise AssertionError(f"Retry activity should not load {model_name} before dispatch")

        def commit(self):
            captured["committed"] = True

    monkeypatch.setattr(
        "orchestrator.temporal.activities.handler_workflow.get_settings",
        lambda: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "orchestrator.temporal.activities.handler_workflow.create_session_factory",
        lambda: lambda: _FakeSessionContextManager(session=_ProjectlessRetrySession()),
    )
    monkeypatch.setattr(
        "orchestrator.temporal.activities.handler_workflow.build_runtime_workflow_handler_registry",
        lambda **kwargs: registry,
    )

    def _retry_use_case(**kwargs):
        captured["kwargs"] = kwargs
        return SimpleNamespace(
            operation_id=operation.operation_id,
            workflow_id=workflow.workflow_id,
            operation_type=operation.operation_type,
            status="retrying",
        )

    monkeypatch.setattr(
        "orchestrator.temporal.activities.handler_workflow.retry_workflow_operation_with_registered_handler",
        _retry_use_case,
    )

    from orchestrator.temporal.activities.handler_workflow import retry_handler_workflow_operation_activity

    result = retry_handler_workflow_operation_activity(
        WorkflowOperationRetryInput(workflow_id=workflow.workflow_id, operation_id=operation.operation_id)
    )

    assert result.workflow_id == workflow.workflow_id
    assert result.operation_id == operation.operation_id
    assert result.operation_status == "retrying"
    assert captured["kwargs"]["handler_registry"] is registry
    assert captured["committed"] is True


def test_retry_handler_workflow_operation_activity_raises_retryable_application_error(monkeypatch):
    workflow = SimpleNamespace(
        workflow_id="parent_planning:MAB-233",
        workflow_type_key="parent_planning",
        tenant_id="tenant-a",
        project_id="project-a",
        status="failed",
        active_run_id=None,
        last_error="operation failed",
    )
    operation = SimpleNamespace(
        operation_id="operation-backlog-planning",
        workflow_id=workflow.workflow_id,
        operation_type="backlog_planning",
    )
    registry = SimpleNamespace(resolve_operation_retry_handler=lambda _handler_key: None)

    class _RetrySession:
        def get(self, model, key):
            model_name = getattr(model, "__name__", "")
            if model_name == "WorkflowExecution":
                return workflow if key == workflow.workflow_id else None
            if model_name == "WorkflowOperation":
                return operation if key == operation.operation_id else None
            raise AssertionError(f"Unexpected model lookup {model_name}")

        def commit(self):
            pass

    monkeypatch.setattr(
        "orchestrator.temporal.activities.handler_workflow.get_settings",
        lambda: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "orchestrator.temporal.activities.handler_workflow.create_session_factory",
        lambda: lambda: _FakeSessionContextManager(session=_RetrySession()),
    )
    monkeypatch.setattr(
        "orchestrator.temporal.activities.handler_workflow.build_runtime_workflow_handler_registry",
        lambda **kwargs: registry,
    )
    monkeypatch.setattr(
        "orchestrator.temporal.activities.handler_workflow.retry_workflow_operation_with_registered_handler",
        lambda **_kwargs: (_ for _ in ()).throw(
            RetryableSpecialistPlanningContractError(
                "Codex returned engineering_planning child_ticket_specs[1] without done_means"
            )
        ),
    )

    from orchestrator.temporal.activities.handler_workflow import retry_handler_workflow_operation_activity

    with pytest.raises(ApplicationError) as exc_info:
        retry_handler_workflow_operation_activity(
            WorkflowOperationRetryInput(workflow_id=workflow.workflow_id, operation_id=operation.operation_id)
        )

    assert exc_info.value.type == "retryable_invalid_model_output"
    assert exc_info.value.non_retryable is False


def test_temporal_registry_includes_parent_planning_and_pr_remediation():
    from orchestrator.temporal.workflow_registry import resolve_temporal_binding_for_handler

    assert resolve_temporal_binding_for_handler(handler_key="jira_parent_feature").handler_key == "jira_parent_feature"
    assert resolve_temporal_binding_for_handler(handler_key="pr_remediation").handler_key == "pr_remediation"


def test_project_deployment_setup_workflow_accepts_temporal_dict_activity_result(monkeypatch):
    workflow_defn = ProjectDeploymentSetupWorkflow()

    async def _fake_execute_activity(_name, _payload, *, start_to_close_timeout, retry_policy=None):  # noqa: ANN001
        assert start_to_close_timeout == timedelta(seconds=321)
        assert retry_policy is not None
        return {
            "workflow_id": "deploy-setup:abc123",
            "tenant_id": "tenant-1",
            "project_id": "project-1",
            "status": "completed",
            "app_ids": ["app-1"],
            "release_ids": ["release-1"],
            "analysis_run_id": "deploy-setup:abc123",
            "last_error": None,
        }

    monkeypatch.setattr(
        "orchestrator.temporal.workflows.project_deployment_setup.workflow.execute_activity",
        _fake_execute_activity,
    )

    state = asyncio.run(
        workflow_defn.run(
            ProjectDeploymentSetupWorkflowInput(
                workflow_id="deploy-setup:abc123",
                tenant_id="tenant-1",
                project_id="project-1",
                requested_by_user_id="admin",
                activity_start_to_close_timeout_seconds=321,
            )
        )
    )

    assert state.workflow_id == "deploy-setup:abc123"
    assert state.backend == "temporal"
    assert state.status == "completed"


class _FakeSession:
    def __init__(self, *, tenant, workflow_type, workflow=None) -> None:
        self.tenant = tenant
        self.workflow_type = workflow_type
        self.workflow = workflow
        self.rollback_called = False

    def get(self, model, key):
        model_name = getattr(model, "__name__", "")
        if model_name == "Tenant":
            return self.tenant
        if model_name == "WorkflowExecution" and self.workflow is not None:
            return self.workflow if key == self.workflow.workflow_id else None
        return None

    def commit(self):
        return None

    def rollback(self):
        self.rollback_called = True


class _FakeSessionContextManager:
    def __init__(self, *, session) -> None:
        self._session = session

    def __call__(self):
        return self

    def __enter__(self):
        return self._session

    def __exit__(self, exc_type, exc, tb):
        return False
