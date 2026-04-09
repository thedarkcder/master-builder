from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TeamRunWorkflowInput:
    run_id: str
    workflow_id: str
    tenant_id: str
    project_id: str | None
    issue_key: str


@dataclass(frozen=True)
class TeamRunTaskCompletionInput:
    operation_id: str
    run_id: str
    task_key: str
    artifact_payload: dict[str, Any] | None
    summary: str | None


@dataclass(frozen=True)
class TeamRunApprovalInput:
    operation_id: str
    run_id: str
    task_key: str
    decision: str
    comment: str | None


@dataclass(frozen=True)
class TeamRunHumanInputInput:
    operation_id: str
    request_id: str


@dataclass(frozen=True)
class TeamRunUpdateResult:
    operation_id: str
    run_id: str
    task_key: str | None
    team_status: str | None
    run_status: str | None


@dataclass(frozen=True)
class TeamRunWorkflowResult:
    run_id: str
    final_status: str | None
