from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from sqlalchemy.orm import Session

from orchestrator.core.config import Settings
from orchestrator.core.workflow_engine import WorkflowEngineState
from orchestrator.core.workflow_engine_factory import (
    build_workflow_engine,
    create_session_factory_for_engine,
)
from orchestrator.core.workflow_operation_service import WorkflowOperationHandle
from orchestrator.core.workflow_type_catalog import get_workflow_type_by_handler_key
from orchestrator.storage.models import Run, RunHumanInputRequest, WorkflowExecution, WorkflowOperation


@dataclass(frozen=True)
class WorkflowRuntimeDeps:
    process_claimed_run_fn: Callable | None
    build_runner_fn: Callable | None
    runtime_kwargs_fn: Callable | None
    retry_workflow_operation_fn: Callable | None = None
    resolve_advance_handler_fn: Callable[[str], "WorkflowAdvanceHandler"] | None = None


@dataclass(frozen=True)
class WorkflowAdvanceRequest:
    workflow_handler_key: str
    tenant_id: str
    tenant: Any
    issue_key: str
    project_id: str | None = None
    issue_summary: str | None = None
    issue_description: object | None = None
    issue_labels: tuple[str, ...] = ()
    payload: dict[str, Any] = field(default_factory=dict)
    webhook_event: str | None = None
    comment_command: str | None = None
    comment_command_argument: str | None = None


@dataclass(frozen=True)
class WorkflowAdvanceResult:
    handled: bool
    reason: str | None = None
    extra: dict[str, object] = field(default_factory=dict)


class WorkflowAdvanceHandler(Protocol):
    def advance(
        self,
        *,
        session: Session,
        settings: Settings,
        workflow_type,
        request: WorkflowAdvanceRequest,
    ) -> WorkflowAdvanceResult:
        ...


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
    ) -> WorkflowAdvanceResult:
        if self._deps.resolve_advance_handler_fn is None:
            raise RuntimeError("Workflow advance handler resolution is not configured")
        workflow_type = get_workflow_type_by_handler_key(
            self._session,
            handler_key=request.workflow_handler_key,
        )
        handler = self._deps.resolve_advance_handler_fn(str(workflow_type.handler_key or "").strip())
        return handler.advance(
            session=self._session,
            settings=self._settings,
            workflow_type=workflow_type,
            request=request,
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
