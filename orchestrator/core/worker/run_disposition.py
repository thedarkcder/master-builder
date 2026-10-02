from __future__ import annotations

from dataclasses import dataclass

from orchestrator.core.workflow.runner import WorkflowResult

RUN_STATUS_SUCCEEDED = "succeeded"
RUN_STATUS_FAILED = "failed"
RUN_STATUS_BLOCKED = "blocked"


@dataclass(frozen=True)
class RunDisposition:
    status: str
    last_error: str | None


def resolve_run_disposition(*, workflow_result: WorkflowResult) -> RunDisposition:
    outcome = str(workflow_result.outcome or "").strip().lower()
    if outcome == "success":
        return RunDisposition(status=RUN_STATUS_SUCCEEDED, last_error=None)
    if outcome == "failed":
        return RunDisposition(
            status=RUN_STATUS_FAILED,
            last_error=(
                workflow_result.diagnostics.message
                if workflow_result.diagnostics is not None
                else (
                    workflow_result.blocker_message
                    or "Workflow failed without diagnostics"
                )
            ),
        )
    if outcome == "blocked":
        return RunDisposition(
            status=RUN_STATUS_BLOCKED,
            last_error=(
                workflow_result.blocker_message
                or (
                    workflow_result.diagnostics.message
                    if workflow_result.diagnostics is not None
                    else None
                )
                or "Workflow blocked without diagnostics"
            ),
        )
    return RunDisposition(
        status=RUN_STATUS_FAILED,
        last_error=f"Unsupported workflow outcome '{workflow_result.outcome}' reached terminalization",
    )
