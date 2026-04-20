from __future__ import annotations

from datetime import timedelta

from orchestrator.core.workflow_engine import WorkflowEngineState
from orchestrator.temporal.activities.handler_workflow import (
    process_handler_workflow_advance_activity,
    retry_handler_workflow_operation_activity,
)
from orchestrator.temporal.payloads import (
    HandlerWorkflowAdvanceInput,
    HandlerWorkflowAdvanceResult,
    HandlerWorkflowRunInput,
    WorkflowOperationRetryInput,
    WorkflowOperationRetryResult,
)

try:  # pragma: no cover - exercised when temporal backend is enabled
    from temporalio import workflow
except ImportError as exc:  # pragma: no cover - exercised when temporal backend is enabled
    raise RuntimeError("Temporal backend requires temporalio to be installed") from exc


@workflow.defn(name="HandlerBackedWorkflow")
class HandlerBackedWorkflow:
    def __init__(self) -> None:
        self._workflow_id: str = ""
        self._handler_key: str = ""
        self._status: str = "queued"
        self._active_run_id: str | None = None
        self._last_error: str | None = None
        self._activity_timeout_seconds: int = 7200

    def _apply_advance_result(self, result: HandlerWorkflowAdvanceResult) -> None:
        self._status = str(result.status or "").strip().lower() or self._status
        self._active_run_id = str(result.active_run_id or "").strip() or self._active_run_id
        self._last_error = str(result.last_error or "").strip() or None

    def _apply_retry_result(self, result: WorkflowOperationRetryResult) -> None:
        self._status = str(result.workflow_status or "").strip().lower() or self._status
        self._active_run_id = str(result.active_run_id or "").strip() or self._active_run_id
        self._last_error = str(result.last_error or "").strip() or None

    async def _advance(self, payload: HandlerWorkflowAdvanceInput) -> HandlerWorkflowAdvanceResult:
        return await workflow.execute_activity(
            process_handler_workflow_advance_activity,
            self._workflow_id,
            payload,
            start_to_close_timeout=timedelta(seconds=self._activity_timeout_seconds),
        )

    async def _retry_operation(self, payload: WorkflowOperationRetryInput) -> WorkflowOperationRetryResult:
        return await workflow.execute_activity(
            retry_handler_workflow_operation_activity,
            payload,
            start_to_close_timeout=timedelta(seconds=self._activity_timeout_seconds),
        )

    @workflow.run
    async def run(self, payload: HandlerWorkflowRunInput) -> WorkflowEngineState:
        self._workflow_id = str(payload.workflow_id or "").strip()
        self._handler_key = str(payload.workflow_handler_key or "").strip()
        self._activity_timeout_seconds = max(1, int(payload.activity_start_to_close_timeout_seconds or 0))
        self._status = "running"
        await workflow.wait_condition(lambda: False)
        return self.describe_state()

    @workflow.update
    async def advance(self, payload: HandlerWorkflowAdvanceInput) -> HandlerWorkflowAdvanceResult:
        result = await self._advance(payload)
        self._apply_advance_result(result)
        return result

    @workflow.update
    async def retry_operation(self, payload: WorkflowOperationRetryInput) -> WorkflowOperationRetryResult:
        result = await self._retry_operation(payload)
        self._apply_retry_result(result)
        return result

    @workflow.query
    def describe_state(self) -> WorkflowEngineState:
        return WorkflowEngineState(
            workflow_id=self._workflow_id,
            backend="temporal",
            status=self._status,
            active_run_id=self._active_run_id,
        )
