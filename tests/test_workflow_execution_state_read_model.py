from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from orchestrator.api.admin.workflow_execution_state_read_model import (
    build_workflow_execution_state_read_model,
    bucket_workflow_steps,
)
from orchestrator.api.schemas import (
    WorkflowStatePathEntryRead,
    WorkflowTypeLifecycleRead,
    WorkflowTypeOperationRead,
    WorkflowTypeRead,
)


def test_bucket_workflow_steps_returns_named_buckets() -> None:
    buckets = bucket_workflow_steps(
        state_path=[
            WorkflowStatePathEntryRead(key="brief", label="Brief", status="completed"),
            WorkflowStatePathEntryRead(key="fanout", label="Fanout", status="failed"),
            WorkflowStatePathEntryRead(key="retry", label="Retry", status="retrying"),
            WorkflowStatePathEntryRead(key="promote", label="Promote", status="pending"),
        ]
    )

    assert buckets.completed == ["Brief"]
    assert buckets.failed == ["Fanout"]
    assert buckets.retrying == ["Retry"]
    assert buckets.pending == ["Promote"]


def test_build_workflow_execution_state_read_model_uses_operation_lifecycle() -> None:
    now = datetime.now(timezone.utc)
    workflow = SimpleNamespace(status="failed")
    workflow_type = WorkflowTypeRead(
        key="parent_planning",
        label="Parent Planning",
        orchestration_backend="database",
        lifecycle=WorkflowTypeLifecycleRead(state_path_kind="operation"),
        operations=[
            WorkflowTypeOperationRead(
                operation_type="brief_normalization",
                label="Brief normalization",
                completion_required=True,
            ),
            WorkflowTypeOperationRead(
                operation_type="child_fanout",
                label="Child fanout",
                completion_required=True,
            ),
        ],
    )
    operations = [
        SimpleNamespace(
            operation_type="brief_normalization",
            status="completed",
            summary="Brief is ready.",
            created_at=now,
            started_at=now,
            finished_at=now,
        ),
        SimpleNamespace(
            operation_type="child_fanout",
            status="failed",
            summary="Child planning contract invalid.",
            created_at=now,
            started_at=now,
            finished_at=now,
        ),
    ]

    read_model = build_workflow_execution_state_read_model(
        workflow=workflow,
        workflow_type=workflow_type,
        latest_run=None,
        operations=operations,
        pending_request=None,
        checkpoint_kinds=["execution"],
        runs=[SimpleNamespace(entry_mode="fresh")],
    )

    assert [entry.key for entry in read_model.state_path] == ["brief_normalization", "child_fanout"]
    assert read_model.step_buckets.completed == ["Brief normalization"]
    assert read_model.step_buckets.failed == ["Child fanout"]
    assert read_model.next_step is None
    assert read_model.can_resume is True
    assert read_model.conditional_branches_taken == ["fresh"]
