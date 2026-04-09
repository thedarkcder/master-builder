from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from orchestrator.temporal.activities.team_run import (
    complete_team_task_activity,
    execute_ready_team_task_activity,
    initialize_team_run_activity,
    resume_team_human_input_activity,
    submit_team_approval_activity,
)
from orchestrator.temporal.team_run_payloads import (
    TeamRunApprovalInput,
    TeamRunHumanInputInput,
    TeamRunTaskCompletionInput,
    TeamRunUpdateResult,
    TeamRunWorkflowInput,
    TeamRunWorkflowResult,
)


@workflow.defn
class TeamRunWorkflow:
    def __init__(self) -> None:
        self._pending_operations: list[tuple[str, object]] = []
        self._processed_operations: dict[str, TeamRunUpdateResult] = {}
        self._last_result: TeamRunUpdateResult | None = None

    @workflow.run
    async def run(self, workflow_input: TeamRunWorkflowInput) -> TeamRunWorkflowResult:
        self._last_result = await workflow.execute_activity(
            initialize_team_run_activity,
            workflow_input.run_id,
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
        while str(self._last_result.run_status or "").strip().lower() not in {"succeeded", "blocked", "failed", "cancelled"}:
            while True:
                auto_result = await workflow.execute_activity(
                    execute_ready_team_task_activity,
                    workflow_input.run_id,
                    start_to_close_timeout=timedelta(minutes=30),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
                if auto_result.task_key is None:
                    break
                self._last_result = auto_result
                if str(self._last_result.run_status or "").strip().lower() in {"succeeded", "blocked", "failed", "cancelled"}:
                    break
            if str(self._last_result.run_status or "").strip().lower() in {"succeeded", "blocked", "failed", "cancelled"}:
                break
            await workflow.wait_condition(lambda: len(self._pending_operations) > 0)
            operation_kind, payload = self._pending_operations.pop(0)
            if operation_kind == "complete_task":
                self._last_result = await workflow.execute_activity(
                    complete_team_task_activity,
                    payload,
                    start_to_close_timeout=timedelta(minutes=5),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
            elif operation_kind == "submit_human_input":
                self._last_result = await workflow.execute_activity(
                    resume_team_human_input_activity,
                    payload,
                    start_to_close_timeout=timedelta(minutes=5),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
            else:
                self._last_result = await workflow.execute_activity(
                    submit_team_approval_activity,
                    payload,
                    start_to_close_timeout=timedelta(minutes=5),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
            self._processed_operations[self._last_result.operation_id] = self._last_result
        return TeamRunWorkflowResult(
            run_id=workflow_input.run_id,
            final_status=self._last_result.run_status if self._last_result is not None else None,
        )

    @workflow.update
    async def complete_task(self, payload: TeamRunTaskCompletionInput) -> TeamRunUpdateResult:
        self._pending_operations.append(("complete_task", payload))
        await workflow.wait_condition(lambda: payload.operation_id in self._processed_operations)
        return self._processed_operations[payload.operation_id]

    @workflow.update
    async def submit_approval(self, payload: TeamRunApprovalInput) -> TeamRunUpdateResult:
        self._pending_operations.append(("submit_approval", payload))
        await workflow.wait_condition(lambda: payload.operation_id in self._processed_operations)
        return self._processed_operations[payload.operation_id]

    @workflow.update
    async def submit_human_input(self, payload: TeamRunHumanInputInput) -> TeamRunUpdateResult:
        self._pending_operations.append(("submit_human_input", payload))
        await workflow.wait_condition(lambda: payload.operation_id in self._processed_operations)
        return self._processed_operations[payload.operation_id]
