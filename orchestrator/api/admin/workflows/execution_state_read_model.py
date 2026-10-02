from __future__ import annotations

from dataclasses import dataclass, field

from orchestrator.api.schemas import WorkflowStatePathEntryRead, WorkflowTypeRead
from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot
from orchestrator.core.workflow.transitions import ATTEMPT_ENTRY_MODES
from orchestrator.storage.models import (
    Run,
    RunHumanInputRequest,
    WorkflowExecution,
    WorkflowOperation,
)


@dataclass(frozen=True)
class WorkflowStepBuckets:
    completed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    retrying: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class WorkflowExecutionStateReadModel:
    state_path: list[WorkflowStatePathEntryRead]
    step_buckets: WorkflowStepBuckets
    waiting_on: str | None
    next_step: str | None
    conditional_branches_taken: list[str]
    conditional_branches_available: list[str]
    can_resume: bool
    resume_unavailable_reason: str | None


def workflow_uses_run_state_path(*, workflow_type: WorkflowTypeRead) -> bool:
    return str(workflow_type.lifecycle.state_path_kind or "").strip().lower() == "run"


def build_run_stage_path(*, latest_run: Run | None) -> list[WorkflowStatePathEntryRead]:
    if latest_run is None or not isinstance(latest_run.plan, dict):
        return []
    snapshot = ExecutionSnapshot.require(latest_run.plan, allow_empty=True)
    stages = snapshot.dump().get("stages", {})
    ordered_entries: list[WorkflowStatePathEntryRead] = []
    label_map = {
        "pm": "PM",
        "dev": "Dev",
        "test": "Test",
        "review": "Review",
    }
    for key in ("pm", "dev", "test", "review"):
        payload = stages.get(key, {})
        if not isinstance(payload, dict):
            continue
        status = str(payload.get("status") or "").strip().lower()
        if not status:
            continue
        ordered_entries.append(
            WorkflowStatePathEntryRead(
                key=key,
                label=label_map.get(key, key.replace("_", " ").title()),
                status=status,
                recorded_at=payload.get("completed_at"),
                detail=str(payload.get("summary") or "").strip() or None,
            )
        )
    return ordered_entries


def build_operation_path(
    *,
    workflow_type: WorkflowTypeRead,
    operations: list[WorkflowOperation],
) -> list[WorkflowStatePathEntryRead]:
    status_by_type = {
        str(operation.operation_type or "").strip(): operation
        for operation in operations
        if str(operation.operation_type or "").strip()
    }
    entries: list[WorkflowStatePathEntryRead] = []
    for definition in workflow_type.operations:
        current = status_by_type.get(definition.operation_type)
        status = str(
            current.status
            if current is not None
            else ("pending" if definition.completion_required else "not_started")
        ).strip()
        entries.append(
            WorkflowStatePathEntryRead(
                key=definition.operation_type,
                label=definition.label,
                status=status,
                recorded_at=(
                    current.finished_at or current.started_at or current.created_at
                )
                if current is not None
                else None,
                detail=(
                    current.summary if current is not None else definition.description
                ),
            )
        )
    return entries


def bucket_workflow_steps(
    *, state_path: list[WorkflowStatePathEntryRead]
) -> WorkflowStepBuckets:
    completed: list[str] = []
    failed: list[str] = []
    pending: list[str] = []
    retrying: list[str] = []
    for entry in state_path:
        normalized = str(entry.status or "").strip().lower()
        if normalized in {"completed", "succeeded"}:
            completed.append(entry.label)
        elif normalized == "failed":
            failed.append(entry.label)
        elif normalized == "retrying":
            retrying.append(entry.label)
        elif normalized in {
            "running",
            "queued",
            "waiting_for_input",
            "pending",
            "not_started",
        }:
            pending.append(entry.label)
    return WorkflowStepBuckets(
        completed=completed,
        failed=failed,
        pending=pending,
        retrying=retrying,
    )


def resolve_waiting_on(
    *,
    pending_request: RunHumanInputRequest | None,
    operations: list[WorkflowOperation],
) -> str | None:
    if pending_request is not None:
        return "human_input"
    if any(
        str(operation.status or "").strip().lower() == "retrying"
        for operation in operations
    ):
        return "retry_backoff"
    return None


def resolve_next_step(
    *,
    waiting_on: str | None,
    state_path: list[WorkflowStatePathEntryRead],
    workflow: WorkflowExecution,
) -> str | None:
    if waiting_on == "human_input":
        return "Await human input"
    if waiting_on == "retry_backoff":
        return "Retry failed operation"
    if str(workflow.status or "").strip().lower() in {"completed", "succeeded"}:
        return "Completed"
    for entry in state_path:
        normalized = str(entry.status or "").strip().lower()
        if normalized in {"pending", "not_started", "queued", "running"}:
            return entry.label
    return None


def resolve_branch_sets(*, runs: list[Run]) -> tuple[list[str], list[str]]:
    taken = sorted(
        {
            str(run.entry_mode or "").strip()
            for run in runs
            if str(run.entry_mode or "").strip()
        }
    )
    return taken, [mode for mode in ATTEMPT_ENTRY_MODES]


def resolve_resume_execution_state(
    *,
    workflow: WorkflowExecution,
    checkpoint_kinds: list[str],
) -> tuple[bool, str | None]:
    normalized_status = str(workflow.status or "").strip().lower()
    if normalized_status == "waiting_for_input":
        return False, "Waiting for human input."
    if normalized_status in {"succeeded", "completed", "cancelled"}:
        return False, "Completed executions cannot be restarted."
    if normalized_status not in {"running", "failed"}:
        return False, "Execution is not resumable."
    normalized_checkpoint_kinds = {
        str(kind or "").strip().lower()
        for kind in checkpoint_kinds
        if str(kind).strip()
    }
    if not normalized_checkpoint_kinds.intersection(
        {"pm", "execution", "orchestrated"}
    ):
        return False, "No resumable execution state is available."
    return True, None


def build_workflow_execution_state_read_model(
    *,
    workflow: WorkflowExecution,
    workflow_type: WorkflowTypeRead,
    latest_run: Run | None,
    operations: list[WorkflowOperation],
    pending_request: RunHumanInputRequest | None,
    checkpoint_kinds: list[str],
    runs: list[Run],
) -> WorkflowExecutionStateReadModel:
    state_path = (
        build_run_stage_path(latest_run=latest_run)
        if workflow_uses_run_state_path(workflow_type=workflow_type)
        else build_operation_path(workflow_type=workflow_type, operations=operations)
    )
    waiting_on = resolve_waiting_on(
        pending_request=pending_request, operations=operations
    )
    branches_taken, branches_available = resolve_branch_sets(runs=runs)
    can_resume, resume_unavailable_reason = resolve_resume_execution_state(
        workflow=workflow,
        checkpoint_kinds=checkpoint_kinds,
    )
    return WorkflowExecutionStateReadModel(
        state_path=state_path,
        step_buckets=bucket_workflow_steps(state_path=state_path),
        waiting_on=waiting_on,
        next_step=resolve_next_step(
            waiting_on=waiting_on, state_path=state_path, workflow=workflow
        ),
        conditional_branches_taken=branches_taken,
        conditional_branches_available=branches_available,
        can_resume=can_resume,
        resume_unavailable_reason=resume_unavailable_reason,
    )
