from __future__ import annotations

import asyncio
import threading

from sqlalchemy.orm import Session, sessionmaker

from orchestrator.core.config import Settings
from orchestrator.core.workflow_engine import WorkflowEngineState
from orchestrator.core.workflow_operation_service import WorkflowOperationHandle
from orchestrator.storage.models import Run, RunHumanInputRequest, WorkflowExecution, WorkflowOperation
from orchestrator.temporal.client import connect_temporal_client, temporal_task_queue
from orchestrator.temporal.payloads import (
    DevelopmentTeamRunWorkflowInput,
    HumanInputResumeInput,
)
from orchestrator.temporal.workflows.development_team_run import DevelopmentTeamRunWorkflow

try:  # pragma: no cover - exercised when temporal backend is enabled
    from temporalio.exceptions import WorkflowAlreadyStartedError
except ImportError as exc:  # pragma: no cover - exercised when temporal backend is enabled
    raise RuntimeError("Temporal backend requires temporalio to be installed") from exc


def _temporal_workflow_handle_id(*, workflow_id: str) -> str:
    return f"workflow:{workflow_id}"


def _run_sync(awaitable):  # noqa: ANN001, ANN201
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)

    result: dict[str, object] = {}
    error: dict[str, BaseException] = {}

    def _runner() -> None:
        try:
            result["value"] = asyncio.run(awaitable)
        except BaseException as exc:  # noqa: BLE001
            error["value"] = exc

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join()
    if "value" in error:
        raise error["value"]
    return result.get("value")


class TemporalWorkflowEngine:
    backend = "temporal"

    def __init__(self, *, process_claimed_run_fn, build_runner_fn, runtime_kwargs_fn, retry_workflow_operation_fn=None):
        self._process_claimed_run_fn = process_claimed_run_fn
        self._build_runner_fn = build_runner_fn
        self._runtime_kwargs_fn = runtime_kwargs_fn
        self._retry_workflow_operation_fn = retry_workflow_operation_fn

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
        _ = session, session_factory

        async def _start() -> None:
            client = await connect_temporal_client(settings)
            payload = DevelopmentTeamRunWorkflowInput(
                workflow_id=workflow.workflow_id,
                run_id=run.run_id,
                claim_id=claim_id,
                tenant_id=run.tenant_id,
                project_id=run.project_id,
                issue_key=run.issue_key,
            )
            try:
                await client.start_workflow(
                    DevelopmentTeamRunWorkflow.run,
                    payload,
                    id=_temporal_workflow_handle_id(workflow_id=workflow.workflow_id),
                    task_queue=temporal_task_queue(settings),
                )
            except WorkflowAlreadyStartedError:
                return

        _run_sync(_start())
        return run

    def resume_workflow(
        self,
        *,
        session: Session,
        settings: Settings,
        session_factory: sessionmaker[Session],
        workflow: WorkflowExecution,
        request: RunHumanInputRequest,
    ) -> Run:
        _ = session

        async def _resume() -> str | None:
            client = await connect_temporal_client(settings)
            handle = client.get_workflow_handle_for(
                DevelopmentTeamRunWorkflow,
                _temporal_workflow_handle_id(workflow_id=workflow.workflow_id),
            )
            return await handle.execute_update(
                DevelopmentTeamRunWorkflow.resume_human_input,
                HumanInputResumeInput(request_id=request.request_id),
            )

        resumed_run_id = _run_sync(_resume())
        with session_factory() as resumed_session:
            if resumed_run_id:
                resumed_run = resumed_session.get(Run, resumed_run_id)
                if resumed_run is not None:
                    return resumed_run
            request_row = resumed_session.get(RunHumanInputRequest, request.request_id)
            if request_row is not None and str(request_row.consumed_by_run_id or "").strip():
                resumed_run = resumed_session.get(Run, request_row.consumed_by_run_id)
                if resumed_run is not None:
                    return resumed_run
        raise RuntimeError(f"Temporal workflow {workflow.workflow_id} resumed without creating a run projection")

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
        if self._retry_workflow_operation_fn is None:
            raise RuntimeError("Workflow operation retry is not configured for this workflow engine invocation")
        return self._retry_workflow_operation_fn(
            session=session,
            settings=settings,
            session_factory=session_factory,
            workflow=workflow,
            operation=operation,
        )
