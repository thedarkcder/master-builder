from __future__ import annotations

from sqlalchemy.orm import Session, sessionmaker

from orchestrator.core.config import Settings
from orchestrator.core.legacy_workflow_engine import LegacyWorkflowEngine
from orchestrator.core.workflow_engine import WorkflowEngine
from orchestrator.storage.models import WorkflowExecution


def resolve_workflow_backend(*, settings: Settings, workflow: WorkflowExecution | None = None) -> str:
    backend = str(getattr(workflow, "orchestration_backend", "") or "").strip().lower()
    if backend:
        return backend
    return str(getattr(settings, "orchestration_backend", "legacy") or "legacy").strip().lower()


def build_workflow_engine(
    *,
    settings: Settings,
    workflow: WorkflowExecution | None = None,
    process_claimed_run_fn,
    build_runner_fn,
    runtime_kwargs_fn,
    retry_workflow_operation_fn=None,
) -> WorkflowEngine:
    backend = resolve_workflow_backend(settings=settings, workflow=workflow)
    if backend == "temporal":
        from orchestrator.temporal.workflow_engine import TemporalWorkflowEngine

        return TemporalWorkflowEngine(
            process_claimed_run_fn=process_claimed_run_fn,
            build_runner_fn=build_runner_fn,
            runtime_kwargs_fn=runtime_kwargs_fn,
            retry_workflow_operation_fn=retry_workflow_operation_fn,
        )
    return LegacyWorkflowEngine(
        process_claimed_run_fn=process_claimed_run_fn,
        build_runner_fn=build_runner_fn,
        runtime_kwargs_fn=runtime_kwargs_fn,
        retry_workflow_operation_fn=retry_workflow_operation_fn,
    )


def create_session_factory_for_engine(*, session: Session, settings: Settings) -> sessionmaker[Session]:
    bind = session.get_bind()
    if bind is None:
        raise RuntimeError("Workflow engine requires an active database bind")
    return sessionmaker(bind=bind, expire_on_commit=False)
