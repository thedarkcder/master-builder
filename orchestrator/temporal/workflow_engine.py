from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.orm import Session, sessionmaker

from orchestrator.core.config import Settings
from orchestrator.core.workflow_advance import WorkflowAdvanceOutcome
from orchestrator.core.workflow_engine import WorkflowEngineState
from orchestrator.core.workflow_operation_service import WorkflowOperationHandle
from orchestrator.core.workflow_execution_projection import workflow_execution_id
from orchestrator.core.workflow_type_catalog import get_workflow_type
from orchestrator.storage.models import Run, RunHumanInputRequest, WorkflowExecution, WorkflowOperation
from orchestrator.temporal.client import connect_temporal_client
from orchestrator.temporal.payloads import (
    DevelopmentTeamRunWorkflowInput,
    HandlerWorkflowAdvanceInput,
    HandlerWorkflowRunInput,
    HandlerWorkflowAdvanceResult,
    HumanInputResumeInput,
    WorkflowOperationRetryInput,
    WorkflowOperationRetryResult,
)
from orchestrator.temporal.workflow_registry import resolve_temporal_binding_for_handler

try:  # pragma: no cover - exercised when temporal backend is enabled
    from temporalio.exceptions import WorkflowAlreadyStartedError
except ImportError as exc:  # pragma: no cover - exercised when temporal backend is enabled
    raise RuntimeError("Temporal backend requires temporalio to be installed") from exc


@dataclass(frozen=True)
class TemporalWorkflowConfig:
    workflow_defn: object
    execution_mode: str
    task_queue: str
    workflow_execution_timeout_seconds: int
    workflow_run_timeout_seconds: int
    activity_start_to_close_timeout_seconds: int
    human_input_resume_timeout_seconds: int


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


def _require_positive_temporal_timeout(*, workflow_type_key: str, temporal: dict, field_name: str) -> int:
    try:
        value = int(temporal.get(field_name) or 0)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Workflow type {workflow_type_key} has invalid temporal engine config field {field_name}"
        ) from exc
    if value < 1:
        raise RuntimeError(
            f"Workflow type {workflow_type_key} is missing temporal engine config field {field_name}"
        )
    return value


def _temporal_config_for_workflow(
    *,
    session: Session,
    workflow: WorkflowExecution,
    settings: Settings,
) -> TemporalWorkflowConfig:
    del settings
    workflow_type = get_workflow_type(session, workflow_type_key=workflow.workflow_type_key)
    backend = str(workflow_type.orchestration_backend or "").strip().lower()
    if backend != "temporal":
        raise RuntimeError(
            f"Workflow type {workflow_type.workflow_type_key} is not configured for the temporal engine"
        )
    binding = resolve_temporal_binding_for_handler(handler_key=workflow_type.handler_key)
    temporal = {
        "workflow_execution_timeout_seconds": binding.workflow_execution_timeout_seconds,
        "workflow_run_timeout_seconds": binding.workflow_run_timeout_seconds,
        "activity_start_to_close_timeout_seconds": binding.activity_start_to_close_timeout_seconds,
        "human_input_resume_timeout_seconds": binding.human_input_resume_timeout_seconds,
    }
    return TemporalWorkflowConfig(
        workflow_defn=binding.workflow_defn,
        execution_mode=str(binding.execution_mode or "").strip().lower() or "run",
        task_queue=binding.task_queue,
        workflow_execution_timeout_seconds=_require_positive_temporal_timeout(
            workflow_type_key=workflow_type.workflow_type_key,
            temporal=temporal,
            field_name="workflow_execution_timeout_seconds",
        ),
        workflow_run_timeout_seconds=_require_positive_temporal_timeout(
            workflow_type_key=workflow_type.workflow_type_key,
            temporal=temporal,
            field_name="workflow_run_timeout_seconds",
        ),
        activity_start_to_close_timeout_seconds=_require_positive_temporal_timeout(
            workflow_type_key=workflow_type.workflow_type_key,
            temporal=temporal,
            field_name="activity_start_to_close_timeout_seconds",
        ),
        human_input_resume_timeout_seconds=_require_positive_temporal_timeout(
            workflow_type_key=workflow_type.workflow_type_key,
            temporal=temporal,
            field_name="human_input_resume_timeout_seconds",
        ),
    )


def _temporal_config_for_workflow_type(
    *,
    session: Session,
    workflow_type,
    settings: Settings,
) -> TemporalWorkflowConfig:
    del session, settings
    backend = str(workflow_type.orchestration_backend or "").strip().lower()
    if backend != "temporal":
        raise RuntimeError(
            f"Workflow type {workflow_type.workflow_type_key} is not configured for the temporal engine"
        )
    binding = resolve_temporal_binding_for_handler(handler_key=workflow_type.handler_key)
    temporal = {
        "workflow_execution_timeout_seconds": binding.workflow_execution_timeout_seconds,
        "workflow_run_timeout_seconds": binding.workflow_run_timeout_seconds,
        "activity_start_to_close_timeout_seconds": binding.activity_start_to_close_timeout_seconds,
        "human_input_resume_timeout_seconds": binding.human_input_resume_timeout_seconds,
    }
    return TemporalWorkflowConfig(
        workflow_defn=binding.workflow_defn,
        execution_mode=str(binding.execution_mode or "").strip().lower() or "run",
        task_queue=binding.task_queue,
        workflow_execution_timeout_seconds=_require_positive_temporal_timeout(
            workflow_type_key=workflow_type.workflow_type_key,
            temporal=temporal,
            field_name="workflow_execution_timeout_seconds",
        ),
        workflow_run_timeout_seconds=_require_positive_temporal_timeout(
            workflow_type_key=workflow_type.workflow_type_key,
            temporal=temporal,
            field_name="workflow_run_timeout_seconds",
        ),
        activity_start_to_close_timeout_seconds=_require_positive_temporal_timeout(
            workflow_type_key=workflow_type.workflow_type_key,
            temporal=temporal,
            field_name="activity_start_to_close_timeout_seconds",
        ),
        human_input_resume_timeout_seconds=_require_positive_temporal_timeout(
            workflow_type_key=workflow_type.workflow_type_key,
            temporal=temporal,
            field_name="human_input_resume_timeout_seconds",
        ),
    )


def _handler_backed_workflow_id(*, workflow_type_key: str, issue_key: str) -> str:
    return workflow_execution_id(workflow_type_key=workflow_type_key, issue_key=issue_key)


def _handler_advance_input_from_request(request) -> HandlerWorkflowAdvanceInput:
    return HandlerWorkflowAdvanceInput(
        workflow_handler_key=request.workflow_handler_key,
        tenant_id=request.tenant_id,
        project_id=request.project_id,
        issue_key=request.issue_key,
        issue_summary=request.issue_summary,
        issue_description=request.issue_description,
        issue_labels=tuple(request.issue_labels or ()),
        payload=dict(request.payload or {}),
        webhook_event=request.webhook_event,
        comment_command=request.comment_command,
        comment_command_argument=request.comment_command_argument,
    )


def _workflow_advance_outcome_from_temporal(result: HandlerWorkflowAdvanceResult) -> WorkflowAdvanceOutcome:
    return WorkflowAdvanceOutcome(
        handled=bool(result.handled),
        reason=str(result.reason or "").strip() or None,
    )


async def _ensure_handler_workflow_handle(
    *,
    client,
    config: TemporalWorkflowConfig,
    workflow_id: str,
    workflow_handler_key: str,
):
    run_payload = HandlerWorkflowRunInput(
        workflow_id=workflow_id,
        workflow_handler_key=workflow_handler_key,
        activity_start_to_close_timeout_seconds=config.activity_start_to_close_timeout_seconds,
    )
    try:
        return await client.start_workflow(
            config.workflow_defn.run,
            run_payload,
            id=_temporal_workflow_handle_id(workflow_id=workflow_id),
            task_queue=config.task_queue,
            execution_timeout=timedelta(seconds=config.workflow_execution_timeout_seconds),
            run_timeout=timedelta(seconds=config.workflow_run_timeout_seconds),
        )
    except WorkflowAlreadyStartedError:
        return client.get_workflow_handle_for(
            config.workflow_defn,
            _temporal_workflow_handle_id(workflow_id=workflow_id),
        )


class TemporalWorkflowEngine:
    backend = "temporal"

    def __init__(self, *, process_claimed_run_fn, build_runner_fn, runtime_kwargs_fn, retry_workflow_operation_fn=None):
        self._process_claimed_run_fn = process_claimed_run_fn
        self._build_runner_fn = build_runner_fn
        self._runtime_kwargs_fn = runtime_kwargs_fn
        self._retry_workflow_operation_fn = retry_workflow_operation_fn

    def advance_workflow(
        self,
        *,
        session: Session,
        settings: Settings,
        session_factory: sessionmaker[Session] | None,
        workflow_type,
        request,
        resolve_advance_handler_fn,
    ) -> WorkflowAdvanceOutcome:
        _ = session_factory, resolve_advance_handler_fn
        config = _temporal_config_for_workflow_type(
            session=session,
            workflow_type=workflow_type,
            settings=settings,
        )
        if config.execution_mode != "handler":
            raise RuntimeError(
                f"Temporal advance is not available for workflow handler {workflow_type.handler_key}"
            )
        workflow_id = _handler_backed_workflow_id(
            workflow_type_key=workflow_type.workflow_type_key,
            issue_key=request.issue_key,
        )
        advance_payload = _handler_advance_input_from_request(request)

        async def _advance() -> HandlerWorkflowAdvanceResult:
            client = await connect_temporal_client(settings)
            handle = await _ensure_handler_workflow_handle(
                client=client,
                config=config,
                workflow_id=workflow_id,
                workflow_handler_key=str(workflow_type.handler_key or "").strip(),
            )
            return await handle.execute_update(config.workflow_defn.advance, advance_payload)

        return _workflow_advance_outcome_from_temporal(_run_sync(_advance()))

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
            config = _temporal_config_for_workflow(session=session, workflow=workflow, settings=settings)
            if config.execution_mode != "run":
                raise RuntimeError(
                    f"Temporal start_workflow is not available for workflow type {workflow.workflow_type_key}"
                )
            payload = DevelopmentTeamRunWorkflowInput(
                workflow_id=workflow.workflow_id,
                run_id=run.run_id,
                claim_id=claim_id,
                tenant_id=run.tenant_id,
                project_id=run.project_id,
                issue_key=run.issue_key,
                workflow_execution_timeout_seconds=config.workflow_execution_timeout_seconds,
                workflow_run_timeout_seconds=config.workflow_run_timeout_seconds,
                activity_start_to_close_timeout_seconds=config.activity_start_to_close_timeout_seconds,
                human_input_resume_timeout_seconds=config.human_input_resume_timeout_seconds,
            )
            try:
                await client.start_workflow(
                    config.workflow_defn.run,
                    payload,
                    id=_temporal_workflow_handle_id(workflow_id=workflow.workflow_id),
                    task_queue=config.task_queue,
                    execution_timeout=timedelta(seconds=config.workflow_execution_timeout_seconds),
                    run_timeout=timedelta(seconds=config.workflow_run_timeout_seconds),
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
            config = _temporal_config_for_workflow(session=session, workflow=workflow, settings=settings)
            handle = client.get_workflow_handle_for(
                config.workflow_defn,
                _temporal_workflow_handle_id(workflow_id=workflow.workflow_id),
            )
            return await handle.execute_update(
                config.workflow_defn.resume_human_input,
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
        config = _temporal_config_for_workflow(session=session, workflow=workflow, settings=settings)
        if config.execution_mode == "handler":
            async def _retry() -> WorkflowOperationRetryResult:
                client = await connect_temporal_client(settings)
                workflow_type = get_workflow_type(session, workflow_type_key=workflow.workflow_type_key)
                handle = await _ensure_handler_workflow_handle(
                    client=client,
                    config=config,
                    workflow_id=workflow.workflow_id,
                    workflow_handler_key=str(workflow_type.handler_key or "").strip(),
                )
                return await handle.execute_update(
                    config.workflow_defn.retry_operation,
                    WorkflowOperationRetryInput(
                        workflow_id=workflow.workflow_id,
                        operation_id=operation.operation_id,
                    ),
                )

            result = _run_sync(_retry())
            return WorkflowOperationHandle(
                operation_id=result.operation_id,
                workflow_id=result.workflow_id,
                operation_type=result.operation_type,
                status=result.operation_status,
            )
        if self._retry_workflow_operation_fn is None:
            raise RuntimeError("Workflow operation retry is not configured for this workflow engine invocation")
        return self._retry_workflow_operation_fn(
            session=session,
            settings=settings,
            session_factory=session_factory,
            workflow=workflow,
            operation=operation,
        )
