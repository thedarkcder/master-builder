from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DevelopmentTeamRunWorkflowInput:
    workflow_id: str
    run_id: str
    claim_id: str
    tenant_id: str
    project_id: str | None
    issue_key: str
    workflow_execution_timeout_seconds: int
    workflow_run_timeout_seconds: int
    activity_start_to_close_timeout_seconds: int
    human_input_resume_timeout_seconds: int


@dataclass(frozen=True)
class HumanInputResumeInput:
    request_id: str


@dataclass(frozen=True)
class DevelopmentTeamRunActivityResult:
    workflow_id: str
    run_id: str
    status: str
    issue_key: str
    claim_id: str | None = None
    pending_request_id: str | None = None
    last_error: str | None = None


@dataclass(frozen=True)
class HandlerWorkflowRunInput:
    workflow_id: str
    workflow_handler_key: str
    activity_start_to_close_timeout_seconds: int


@dataclass(frozen=True)
class HandlerWorkflowAdvanceInput:
    workflow_handler_key: str
    tenant_id: str
    project_id: str | None
    issue_key: str
    issue_summary: str | None = None
    issue_description: object | None = None
    issue_labels: tuple[str, ...] = ()
    payload: dict[str, Any] | None = None
    webhook_event: str | None = None
    comment_command: str | None = None
    comment_command_argument: str | None = None


@dataclass(frozen=True)
class HandlerWorkflowAdvanceResult:
    handled: bool
    reason: str | None
    status: str
    active_run_id: str | None = None
    last_error: str | None = None


@dataclass(frozen=True)
class WorkflowOperationRetryInput:
    workflow_id: str
    operation_id: str


@dataclass(frozen=True)
class WorkflowOperationRetryResult:
    operation_id: str
    workflow_id: str
    operation_type: str
    operation_status: str
    workflow_status: str
    active_run_id: str | None = None
    last_error: str | None = None
