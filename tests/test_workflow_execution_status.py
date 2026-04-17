from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from orchestrator.core.workflow_execution_status import (
    mark_workflow_running,
    mark_workflow_waiting_for_input,
    recompute_workflow_status,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _workflow():
    now = _now()
    return SimpleNamespace(
        workflow_id="wf-1",
        workflow_type_key="workflow-type",
        status="pending",
        last_error="stale error",
        started_at=None,
        finished_at=now,
        updated_at=now,
    )


class _FakeExecuteResult:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return self

    def all(self):
        return list(self._values)


class _FakeSession:
    def __init__(self, operations):
        self._operations = operations

    def execute(self, _query):
        return _FakeExecuteResult(self._operations)


def test_mark_workflow_running_clears_failure_state() -> None:
    workflow = _workflow()
    timestamp = _now()

    mark_workflow_running(workflow=workflow, now=timestamp)

    assert workflow.status == "running"
    assert workflow.last_error is None
    assert workflow.started_at == timestamp
    assert workflow.finished_at is None
    assert workflow.updated_at == timestamp


def test_mark_workflow_waiting_for_input_clears_terminal_failure() -> None:
    workflow = _workflow()
    timestamp = _now()

    mark_workflow_waiting_for_input(workflow=workflow, now=timestamp)

    assert workflow.status == "waiting_for_input"
    assert workflow.last_error is None
    assert workflow.finished_at is None
    assert workflow.updated_at == timestamp


def test_recompute_workflow_status_marks_completion_from_required_operations(monkeypatch) -> None:
    workflow = _workflow()
    workflow.status = "running"
    operations = [
        SimpleNamespace(operation_type="jira_parent_update", status="completed", summary="done"),
        SimpleNamespace(operation_type="jira_child_fanout", status="completed", summary="done"),
    ]
    definitions = [
        SimpleNamespace(operation_type="jira_parent_update", required=True),
        SimpleNamespace(operation_type="jira_child_fanout", required=True),
    ]
    monkeypatch.setattr(
        "orchestrator.core.workflow_execution_status.list_workflow_type_operations",
        lambda *args, **kwargs: definitions,
    )

    recompute_workflow_status(
        session=_FakeSession(operations),
        workflow=workflow,
        now=_now(),
    )

    assert workflow.status == "completed"
    assert workflow.last_error is None
    assert workflow.finished_at is not None


def test_recompute_workflow_status_marks_failure_from_required_operation(monkeypatch) -> None:
    workflow = _workflow()
    operations = [
        SimpleNamespace(operation_type="jira_parent_update", status="completed", summary="done"),
        SimpleNamespace(operation_type="jira_child_fanout", status="failed", summary="content limit"),
    ]
    definitions = [
        SimpleNamespace(operation_type="jira_parent_update", required=True),
        SimpleNamespace(operation_type="jira_child_fanout", required=True),
    ]
    monkeypatch.setattr(
        "orchestrator.core.workflow_execution_status.list_workflow_type_operations",
        lambda *args, **kwargs: definitions,
    )

    recompute_workflow_status(
        session=_FakeSession(operations),
        workflow=workflow,
        now=_now(),
    )

    assert workflow.status == "failed"
    assert workflow.last_error == "content limit"
    assert workflow.finished_at is not None
