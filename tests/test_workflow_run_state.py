from __future__ import annotations

from datetime import datetime, timezone

from orchestrator.core.workflow.run_state import (
    project_workflow_for_new_run_attempt,
    project_workflow_for_run_started,
    project_workflow_for_run_terminal,
    project_workflow_for_waiting_input,
    reconcile_workflow_with_active_run,
)
from orchestrator.storage.models import Run, WorkflowExecution


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _workflow() -> WorkflowExecution:
    now = _now()
    return WorkflowExecution(
        workflow_id="workflow-1",
        workflow_type_key="issue_execution",
        tenant_id="tenant-1",
        project_id="project-1",
        source_system="jira",
        source_ref="MB-1",
        orchestration_backend="temporal",
        dedupe_scope="issue_execution",
        status="pending",
        created_at=now,
        updated_at=now,
    )


def _run(*, status: str, last_error: str | None = None) -> Run:
    now = _now()
    return Run(
        run_id="run-1",
        workflow_id="workflow-1",
        tenant_id="tenant-1",
        project_id="project-1",
        issue_key="MB-1",
        attempt_number=1,
        dedupe_scope="issue_execution",
        status=status,
        last_error=last_error,
        created_at=now,
    )


def test_project_workflow_for_new_attempt_resets_failure_fields() -> None:
    workflow = _workflow()
    workflow.status = "failed"
    workflow.last_error = "old failure"
    workflow.finished_at = _now()

    project_workflow_for_new_run_attempt(
        workflow,
        run=_run(status="queued"),
        latest_checkpoint_id="checkpoint-1",
        orchestration_backend="temporal",
        now=_now(),
    )

    assert workflow.status == "queued"
    assert workflow.last_error is None
    assert workflow.finished_at is None
    assert workflow.active_run_id == "run-1"
    assert workflow.latest_checkpoint_id == "checkpoint-1"


def test_project_workflow_for_run_started_sets_running_and_started_at() -> None:
    workflow = _workflow()
    timestamp = _now()

    project_workflow_for_run_started(
        workflow,
        run=_run(status="running"),
        now=timestamp,
    )

    assert workflow.status == "running"
    assert workflow.active_run_id == "run-1"
    assert workflow.started_at == timestamp


def test_project_workflow_for_run_terminal_maps_blocked_run_to_failed_workflow() -> None:
    workflow = _workflow()
    finished_at = _now()
    run = _run(status="blocked", last_error="content_limit")
    run.finished_at = finished_at

    project_workflow_for_run_terminal(workflow, run=run, now=finished_at)

    assert workflow.status == "failed"
    assert workflow.last_error == "content_limit"
    assert workflow.finished_at == finished_at


def test_project_workflow_for_waiting_input_clears_failure_state() -> None:
    workflow = _workflow()
    workflow.status = "failed"
    workflow.last_error = "old failure"
    workflow.finished_at = _now()

    project_workflow_for_waiting_input(
        workflow,
        run=_run(status="waiting_for_input"),
        latest_checkpoint_id="checkpoint-2",
        now=_now(),
    )

    assert workflow.status == "waiting_for_input"
    assert workflow.last_error is None
    assert workflow.finished_at is None
    assert workflow.latest_checkpoint_id == "checkpoint-2"


def test_reconcile_workflow_with_active_run_uses_shared_terminal_mapping() -> None:
    workflow = _workflow()
    run = _run(status="cancelled", last_error="cancelled by user")
    finished_at = _now()
    run.finished_at = finished_at

    reconcile_workflow_with_active_run(
        workflow,
        active_run=run,
        now=finished_at,
    )

    assert workflow.status == "cancelled"
    assert workflow.last_error is None
    assert workflow.finished_at == finished_at
