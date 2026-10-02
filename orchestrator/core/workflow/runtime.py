from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from sqlalchemy.orm import Session

from orchestrator.core.config import Settings
from orchestrator.core.workflow.advance import (
    WorkflowAdvanceLifecycle,
    WorkflowAdvanceHandler,
    WorkflowAdvanceOutcome,
    WorkflowAdvanceRequest,
    WorkflowTrigger,
)
from orchestrator.core.workflow.engine import WorkflowEngineState
from orchestrator.core.workflow.engine_factory import (
    build_workflow_engine,
    create_session_factory_for_engine,
)
from orchestrator.core.workflow.handler_registry import WorkflowHandlerRegistry
from orchestrator.core.observability.otel_telemetry import telemetry_span
from orchestrator.core.runs.service import (
    EnqueueRunResult,
    RunBootstrap,
    enqueue_attempt_for_workflow,
    enqueue_attempt_for_workflow_uncommitted,
)
from orchestrator.core.workflow.operation_service import WorkflowOperationHandle
from orchestrator.core.workflow.type_catalog import get_workflow_type_by_handler_key
from orchestrator.storage.models import (
    Run,
    RunHumanInputRequest,
    WorkflowExecution,
    WorkflowOperation,
)

__all__ = [
    "WorkflowAdvanceHandler",
    "WorkflowAdvanceLifecycle",
    "WorkflowAdvanceOutcome",
    "WorkflowAdvanceRequest",
    "WorkflowTrigger",
    "WorkflowRuntime",
    "WorkflowRuntimeDeps",
    "build_workflow_runtime",
]


@dataclass(frozen=True)
class WorkflowRuntimeDeps:
    process_claimed_run_fn: Callable | None
    build_runner_fn: Callable | None
    runtime_kwargs_fn: Callable | None
    resolve_advance_handler_fn: Callable[[str], "WorkflowAdvanceHandler"] | None = None
    workflow_handler_registry: WorkflowHandlerRegistry | None = None


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
            workflow_handler_registry=self._deps.workflow_handler_registry,
        )

    def advance(
        self,
        *,
        request: WorkflowAdvanceRequest,
    ) -> WorkflowAdvanceOutcome:
        with telemetry_span(
            "workflow_runtime.advance",
            attributes={
                "workflow.handler_key": request.workflow_handler_key,
                "workflow.execution_key": request.execution.key,
                "workflow.source_system": request.execution.source.source_system,
                "workflow.source_ref": request.execution.source.source_ref,
                "orchestration.backend": str(
                    getattr(self._settings, "orchestration_backend", "") or ""
                ),
            },
        ):
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
        workflow_id = str(getattr(workflow, "workflow_id", "") or "").strip()
        workflow_type_key = str(
            getattr(workflow, "workflow_type_key", "") or ""
        ).strip()
        tenant_id = str(getattr(workflow, "tenant_id", "") or "").strip()
        project_id = str(getattr(workflow, "project_id", "") or "").strip()
        run_id = str(getattr(run, "run_id", "") or "").strip()
        with telemetry_span(
            "workflow_runtime.start_execution",
            attributes={
                "workflow.id": workflow_id,
                "workflow.type": workflow_type_key,
                "run.id": run_id,
                "tenant.id": tenant_id,
                "project.id": project_id,
            },
        ):
            return self._engine(workflow=workflow).start_workflow(
                session=self._session,
                settings=self._settings,
                session_factory=create_session_factory_for_engine(
                    session=self._session, settings=self._settings
                ),
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
        project_id = str(getattr(bootstrap, "project_id", "") or "").strip()
        tenant_id = str(getattr(bootstrap, "tenant_id", "") or "").strip()
        issue_key = str(getattr(bootstrap, "issue_key", "") or "").strip()
        with telemetry_span(
            "workflow_runtime.create_attempt",
            attributes={
                "workflow.id": workflow_id,
                "project.id": project_id,
                "tenant.id": tenant_id,
                "run.issue_key": issue_key,
                "workflow.commit_immediately": commit,
            },
        ):
            enqueue = (
                enqueue_attempt_for_workflow
                if commit
                else enqueue_attempt_for_workflow_uncommitted
            )
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
        workflow_id = str(getattr(workflow, "workflow_id", "") or "").strip()
        workflow_type_key = str(
            getattr(workflow, "workflow_type_key", "") or ""
        ).strip()
        tenant_id = str(getattr(workflow, "tenant_id", "") or "").strip()
        request_id = str(getattr(request, "request_id", "") or "").strip()
        with telemetry_span(
            "workflow_runtime.resume_input",
            attributes={
                "workflow.id": workflow_id,
                "workflow.type": workflow_type_key,
                "request.id": request_id,
                "tenant.id": tenant_id,
            },
        ):
            return self._engine(workflow=workflow).resume_workflow(
                session=self._session,
                settings=self._settings,
                session_factory=create_session_factory_for_engine(
                    session=self._session, settings=self._settings
                ),
                workflow=workflow,
                request=request,
            )

    def query_execution(
        self,
        *,
        workflow: WorkflowExecution,
    ) -> WorkflowEngineState:
        workflow_id = str(getattr(workflow, "workflow_id", "") or "").strip()
        workflow_type_key = str(
            getattr(workflow, "workflow_type_key", "") or ""
        ).strip()
        tenant_id = str(getattr(workflow, "tenant_id", "") or "").strip()
        with telemetry_span(
            "workflow_runtime.query_execution",
            attributes={
                "workflow.id": workflow_id,
                "workflow.type": workflow_type_key,
                "tenant.id": tenant_id,
            },
        ):
            return self._engine(workflow=workflow).query_workflow(workflow=workflow)

    def retry_operation(
        self,
        *,
        workflow: WorkflowExecution,
        operation: WorkflowOperation,
    ) -> WorkflowOperationHandle:
        workflow_id = str(getattr(workflow, "workflow_id", "") or "").strip()
        workflow_type_key = str(
            getattr(workflow, "workflow_type_key", "") or ""
        ).strip()
        tenant_id = str(getattr(workflow, "tenant_id", "") or "").strip()
        operation_id = str(getattr(operation, "operation_id", "") or "").strip()
        operation_type = str(getattr(operation, "operation_type", "") or "").strip()
        with telemetry_span(
            "workflow_runtime.retry_operation",
            attributes={
                "workflow.id": workflow_id,
                "workflow.type": workflow_type_key,
                "operation.id": operation_id,
                "operation.type": operation_type,
                "tenant.id": tenant_id,
            },
        ):
            return self._engine(workflow=workflow).retry_workflow_operation(
                session=self._session,
                settings=self._settings,
                session_factory=create_session_factory_for_engine(
                    session=self._session, settings=self._settings
                ),
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
    resolve_advance_handler_fn=None,
    workflow_handler_registry: WorkflowHandlerRegistry | None = None,
) -> WorkflowRuntime:
    return WorkflowRuntime(
        session=session,
        settings=settings,
        deps=WorkflowRuntimeDeps(
            process_claimed_run_fn=process_claimed_run_fn,
            build_runner_fn=build_runner_fn,
            runtime_kwargs_fn=runtime_kwargs_fn,
            resolve_advance_handler_fn=resolve_advance_handler_fn,
            workflow_handler_registry=workflow_handler_registry,
        ),
    )
