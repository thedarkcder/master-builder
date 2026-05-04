from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy.orm import Session, sessionmaker

from orchestrator.core.config import Settings
from orchestrator.core.workflow.advance import execute_workflow_advance
from orchestrator.core.workflow.engine import WorkflowEngineState
from orchestrator.core.workflow.handler_registry import WorkflowHandlerRegistry
from orchestrator.core.workflow.operation_retry_use_case import retry_workflow_operation_with_registered_handler
from orchestrator.core.workflow.operation_service import WorkflowOperationHandle
from orchestrator.storage.models import Run, RunHumanInputRequest, Tenant, WorkflowExecution, WorkflowOperation


class LegacyWorkflowEngine:
    backend = "legacy"

    def __init__(self, *, process_claimed_run_fn, build_runner_fn, runtime_kwargs_fn, workflow_handler_registry=None):
        self._process_claimed_run_fn = process_claimed_run_fn
        self._build_runner_fn = build_runner_fn
        self._runtime_kwargs_fn = runtime_kwargs_fn
        self._workflow_handler_registry: WorkflowHandlerRegistry | None = workflow_handler_registry

    def advance_workflow(
        self,
        *,
        session: Session,
        settings: Settings,
        session_factory: sessionmaker[Session] | None,
        workflow_type,
        request,
        resolve_advance_handler_fn,
    ):
        _ = session_factory
        if resolve_advance_handler_fn is None:
            raise RuntimeError("Workflow advance handler resolution is not configured")
        return execute_workflow_advance(
            session=session,
            settings=settings,
            workflow_type=workflow_type,
            request=request,
            resolve_advance_handler_fn=resolve_advance_handler_fn,
        )

    def start_workflow(
        self,
        *,
        session: Session,
        settings: Settings,
        session_factory: sessionmaker[Session],
        workflow: WorkflowExecution,
        run: Run,
        claim_id: str,
    ) -> Run:
        _ = settings, session_factory, claim_id
        tenant = session.get(Tenant, run.tenant_id)
        if tenant is None:
            raise RuntimeError(f"Tenant {run.tenant_id} missing for workflow {workflow.workflow_id}")
        runner = self._build_runner_fn(session=session)
        result = self._process_claimed_run_fn(
            session=session,
            runner=runner,
            settings=settings,
            selection=SimpleNamespace(run=run, tenant=tenant, terminal_run=None),
            **self._runtime_kwargs_fn(session=session, settings=settings),
        )
        if result is None:
            raise RuntimeError(f"Legacy workflow engine did not process run {run.run_id}")
        return result

    def resume_workflow(
        self,
        *,
        session: Session,
        settings: Settings,
        session_factory: sessionmaker[Session],
        workflow: WorkflowExecution,
        request: RunHumanInputRequest,
    ) -> Run:
        _ = workflow, session_factory
        from orchestrator.core.runs.human_input_service import resume_run_from_human_input_answer

        return resume_run_from_human_input_answer(
            session=session,
            settings=settings,
            request=request,
        )

    def query_workflow(
        self,
        *,
        workflow: WorkflowExecution,
    ) -> WorkflowEngineState:
        return WorkflowEngineState(
            workflow_id=workflow.workflow_id,
            backend=self.backend,
            status=workflow.status,
            active_run_id=workflow.active_run_id,
        )

    def retry_workflow_operation(
        self,
        *,
        session: Session,
        settings: Settings,
        session_factory: sessionmaker[Session],
        workflow: WorkflowExecution,
        operation: WorkflowOperation,
    ) -> WorkflowOperationHandle:
        if self._workflow_handler_registry is None:
            raise RuntimeError("Workflow operation retry handler registry is not configured")
        return retry_workflow_operation_with_registered_handler(
            session=session,
            settings=settings,
            session_factory=session_factory,
            workflow=workflow,
            operation=operation,
            handler_registry=self._workflow_handler_registry,
        )
