from __future__ import annotations

from datetime import timedelta

from orchestrator.core.workflow_engine import WorkflowEngineState
from orchestrator.temporal.activities.run_execution import (
    execute_claimed_run_activity,
    resume_human_input_activity,
)
from orchestrator.temporal.payloads import (
    DevelopmentTeamRunActivityResult,
    DevelopmentTeamRunWorkflowInput,
    HumanInputResumeInput,
)

try:  # pragma: no cover - exercised when temporal backend is enabled
    from temporalio import workflow
except ImportError as exc:  # pragma: no cover - exercised when temporal backend is enabled
    raise RuntimeError("Temporal backend requires temporalio to be installed") from exc


@workflow.defn(name="DevelopmentTeamRunWorkflow")
class DevelopmentTeamRunWorkflow:
    def __init__(self) -> None:
        self._workflow_id: str = ""
        self._issue_key: str = ""
        self._status: str = "queued"
        self._active_run_id: str | None = None
        self._pending_request_id: str | None = None
        self._last_error: str | None = None

    def _apply_result(self, result: DevelopmentTeamRunActivityResult) -> None:
        self._workflow_id = str(result.workflow_id or "").strip() or self._workflow_id
        self._issue_key = str(result.issue_key or "").strip() or self._issue_key
        self._active_run_id = str(result.run_id or "").strip() or self._active_run_id
        self._status = str(result.status or "").strip().lower() or self._status
        self._pending_request_id = str(result.pending_request_id or "").strip() or None
        self._last_error = str(result.last_error or "").strip() or None

    async def _execute_initial_run(
        self,
        payload: DevelopmentTeamRunWorkflowInput,
    ) -> DevelopmentTeamRunActivityResult:
        return await workflow.execute_activity(
            execute_claimed_run_activity,
            payload,
            start_to_close_timeout=timedelta(hours=2),
        )

    async def _resume_from_human_input(
        self,
        payload: HumanInputResumeInput,
    ) -> DevelopmentTeamRunActivityResult:
        return await workflow.execute_activity(
            resume_human_input_activity,
            payload,
            start_to_close_timeout=timedelta(hours=2),
        )

    @workflow.run
    async def run(self, payload: DevelopmentTeamRunWorkflowInput) -> WorkflowEngineState:
        self._workflow_id = str(payload.workflow_id or "").strip()
        self._issue_key = str(payload.issue_key or "").strip()
        self._active_run_id = str(payload.run_id or "").strip()
        self._status = "dispatching"
        self._apply_result(await self._execute_initial_run(payload))
        while self._status == "waiting_for_input":
            await workflow.wait_condition(lambda: self._status != "waiting_for_input")
        return self.describe_state()

    @workflow.update
    async def resume_human_input(self, payload: HumanInputResumeInput) -> str | None:
        if self._status != "waiting_for_input":
            return self._active_run_id
        normalized_request_id = str(payload.request_id or "").strip()
        if self._pending_request_id and normalized_request_id != self._pending_request_id:
            raise ValueError(
                f"Workflow {self._workflow_id} is waiting on {self._pending_request_id}, not {normalized_request_id}"
            )
        self._apply_result(await self._resume_from_human_input(payload))
        return self._active_run_id

    @workflow.query
    def describe_state(self) -> WorkflowEngineState:
        return WorkflowEngineState(
            workflow_id=self._workflow_id,
            backend="temporal",
            status=self._status,
            active_run_id=self._active_run_id,
        )
