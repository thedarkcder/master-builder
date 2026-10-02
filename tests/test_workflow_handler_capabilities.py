from __future__ import annotations

from types import SimpleNamespace

from orchestrator.temporal.payloads import WorkflowOperationRetryInput


def test_temporal_retry_activity_dispatches_projectless_workflow(monkeypatch):
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
        workflow_id=workflow.workflow_id,
        operation_type="tenant_operation",
    )
    registry = SimpleNamespace(
        resolve_operation_retry_handler=lambda _handler_key: None
    )
    captured: dict[str, object] = {}

    class ProjectlessRetrySession:
        def get(self, model, key):
            model_name = getattr(model, "__name__", "")
            if model_name == "WorkflowExecution":
                return workflow if key == workflow.workflow_id else None
            if model_name == "WorkflowOperation":
                return operation if key == operation.operation_id else None
            raise AssertionError(
                f"Retry activity should not load {model_name} before handler dispatch"
            )

        def commit(self):
            captured["committed"] = True

    class SessionContextManager:
        def __enter__(self):
            return ProjectlessRetrySession()

        def __exit__(self, exc_type, exc, tb):
            return False

    def retry_use_case(**kwargs):
        captured["kwargs"] = kwargs
        return SimpleNamespace(
            operation_id=operation.operation_id,
            workflow_id=workflow.workflow_id,
            operation_type=operation.operation_type,
            status="retrying",
        )

    monkeypatch.setattr(
        "orchestrator.temporal.activities.handler_workflow.get_settings",
        lambda: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "orchestrator.temporal.activities.handler_workflow.create_session_factory",
        lambda: lambda: SessionContextManager(),
    )
    monkeypatch.setattr(
        "orchestrator.temporal.activities.handler_workflow.build_runtime_workflow_handler_registry",
        lambda **_kwargs: registry,
    )
    monkeypatch.setattr(
        "orchestrator.temporal.activities.handler_workflow.retry_workflow_operation_with_registered_handler",
        retry_use_case,
    )

    from orchestrator.temporal.activities.handler_workflow import (
        retry_handler_workflow_operation_activity,
    )

    result = retry_handler_workflow_operation_activity(
        WorkflowOperationRetryInput(
            workflow_id=workflow.workflow_id, operation_id=operation.operation_id
        )
    )

    assert result.workflow_id == workflow.workflow_id
    assert result.operation_id == operation.operation_id
    assert result.operation_status == "retrying"
    assert captured["kwargs"]["handler_registry"] is registry
    assert captured["committed"] is True
