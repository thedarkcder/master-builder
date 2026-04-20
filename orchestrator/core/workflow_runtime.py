from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from sqlalchemy.orm import Session

from orchestrator.core.config import Settings
from orchestrator.core.workflow_advance import (
    WorkflowAdvanceHandler,
    WorkflowAdvanceOutcome,
    WorkflowAdvanceRequest,
    WorkflowTransitionPlan,
    WorkflowTransitionPlanner,
    apply_workflow_transition_plan,
)
from orchestrator.core.workflow_engine import WorkflowEngineState
from orchestrator.core.workflow_engine_factory import (
    build_workflow_engine,
    create_session_factory_for_engine,
)
from orchestrator.core.runs import (
    EnqueueRunResult,
    RunBootstrap,
    enqueue_attempt_for_workflow,
    enqueue_attempt_for_workflow_uncommitted,
)
from orchestrator.core.workflow_operation_service import WorkflowOperationHandle
from orchestrator.core.workflow_type_catalog import get_workflow_type_by_handler_key
from orchestrator.storage.models import Run, RunHumanInputRequest, WorkflowExecution, WorkflowOperation

__all__ = [
    "WorkflowAdvanceHandler",
    "WorkflowAdvanceOutcome",
    "WorkflowAdvanceRequest",
    "WorkflowRuntime",
    "WorkflowRuntimeDeps",
    "WorkflowTransitionPlan",
    "WorkflowTransitionPlanner",
    "apply_workflow_transition_plan",
    "build_workflow_runtime",
]


@dataclass(frozen=True)
class WorkflowRuntimeDeps:
    process_claimed_run_fn: Callable | None
    build_runner_fn: Callable | None
    runtime_kwargs_fn: Callable | None
    retry_workflow_operation_fn: Callable | None = None
    resolve_advance_handler_fn: Callable[[str], "WorkflowAdvanceHandler"] | None = None


class WorkflowRuntime:
    def __init__(
        self,
        *,
        session: Session,
        settings: Settings,
        deps: WorkflowRuntimeDeps,
    ) -> None:
        self._session = session
        self._settings = settings
        self._deps = deps

    def _engine(self, *, workflow: WorkflowExecution):
        return build_workflow_engine(
            settings=self._settings,
            workflow=workflow,
            process_claimed_run_fn=self._deps.process_claimed_run_fn,
            build_runner_fn=self._deps.build_runner_fn,
            runtime_kwargs_fn=self._deps.runtime_kwargs_fn,
            retry_workflow_operation_fn=self._deps.retry_workflow_operation_fn,
        )

    def advance(
        self,
        *,
        request: WorkflowAdvanceRequest,
    ) -> WorkflowAdvanceOutcome:
        workflow_type = get_workflow_type_by_handler_key(
            self._session,
            handler_key=request.workflow_handler_key,
        )
        return self._engine(workflow=workflow_type).advance_workflow(
            session=self._session,
            settings=self._settings,
            session_factory=None,
            workflow_type=workflow_type,
            request=request,
            resolve_advance_handler_fn=self._deps.resolve_advance_handler_fn,
        )

    def start_execution(
        self,
        *,
        workflow: WorkflowExecution,
        run: Run,
        claim_id: str,
    ) -> Run:
        return self._engine(workflow=workflow).start_workflow(
            session=self._session,
            settings=self._settings,
            session_factory=create_session_factory_for_engine(session=self._session, settings=self._settings),
            workflow=workflow,
            run=run,
            claim_id=claim_id,
        )

    def create_attempt(
        self,
        *,
        workflow_id: str,
        bootstrap: RunBootstrap,
        commit: bool = True,
    ) -> EnqueueRunResult:
        enqueue = enqueue_attempt_for_workflow if commit else enqueue_attempt_for_workflow_uncommitted
        return enqueue(
            self._session,
            workflow_id=workflow_id,
            bootstrap=bootstrap,
        )

    def resume_input(
        self,
        *,
        workflow: WorkflowExecution,
        request: RunHumanInputRequest,
    ) -> Run:
        return self._engine(workflow=workflow).resume_workflow(
            session=self._session,
            settings=self._settings,
            session_factory=create_session_factory_for_engine(session=self._session, settings=self._settings),
            workflow=workflow,
            request=request,
        )

    def query_execution(
        self,
        *,
        workflow: WorkflowExecution,
    ) -> WorkflowEngineState:
        return self._engine(workflow=workflow).query_workflow(workflow=workflow)

    def retry_operation(
        self,
        *,
        workflow: WorkflowExecution,
        operation: WorkflowOperation,
    ) -> WorkflowOperationHandle:
        return self._engine(workflow=workflow).retry_workflow_operation(
            session=self._session,
            settings=self._settings,
            session_factory=create_session_factory_for_engine(session=self._session, settings=self._settings),
            workflow=workflow,
            operation=operation,
        )


def build_workflow_runtime(
    *,
    session: Session,
    settings: Settings,
    process_claimed_run_fn,
    build_runner_fn,
    runtime_kwargs_fn,
    retry_workflow_operation_fn=None,
    resolve_advance_handler_fn=None,
) -> WorkflowRuntime:
    return WorkflowRuntime(
        session=session,
        settings=settings,
        deps=WorkflowRuntimeDeps(
            process_claimed_run_fn=process_claimed_run_fn,
            build_runner_fn=build_runner_fn,
            runtime_kwargs_fn=runtime_kwargs_fn,
            retry_workflow_operation_fn=retry_workflow_operation_fn,
            resolve_advance_handler_fn=resolve_advance_handler_fn,
        ),
    )
