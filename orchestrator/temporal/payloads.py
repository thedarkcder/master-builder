from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DevelopmentTeamRunWorkflowInput:
    workflow_id: str
    run_id: str
    claim_id: str
    tenant_id: str
    project_id: str | None
    issue_key: str


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
