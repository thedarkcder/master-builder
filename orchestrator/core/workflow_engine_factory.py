from __future__ import annotations

from sqlalchemy.orm import Session, sessionmaker

from orchestrator.core.config import Settings
from orchestrator.core.legacy_workflow_engine import LegacyWorkflowEngine
from orchestrator.core.workflow_engine import WorkflowEngine
from orchestrator.storage.models import WorkflowExecution


def resolve_workflow_backend(*, workflow: WorkflowExecution) -> str:
    return str(workflow.orchestration_backend).strip().lower()


def build_workflow_engine(
    *,
    settings,
    workflow: WorkflowExecution,
    process_claimed_run_fn,
    build_runner_fn,
    runtime_kwargs_fn,
    workflow_handler_registry,
) -> WorkflowEngine:
    _ = settings
    backend = resolve_workflow_backend(workflow=workflow)
    if backend == "temporal":
        from orchestrator.temporal.workflow_engine import TemporalWorkflowEngine

        return TemporalWorkflowEngine(
            process_claimed_run_fn=process_claimed_run_fn,
            build_runner_fn=build_runner_fn,
            runtime_kwargs_fn=runtime_kwargs_fn,
            workflow_handler_registry=workflow_handler_registry,
        )
    return LegacyWorkflowEngine(
        process_claimed_run_fn=process_claimed_run_fn,
        build_runner_fn=build_runner_fn,
        runtime_kwargs_fn=runtime_kwargs_fn,
        workflow_handler_registry=workflow_handler_registry,
    )


def create_session_factory_for_engine(*, session: Session, settings: Settings) -> sessionmaker[Session]:
    bind = session.get_bind()
    if bind is None:
        raise RuntimeError("Workflow engine requires an active database bind")
    return sessionmaker(bind=bind, expire_on_commit=False)
