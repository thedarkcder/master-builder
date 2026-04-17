from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.orm import Session, sessionmaker

from orchestrator.core.config import Settings
from orchestrator.storage.models import Run, RunHumanInputRequest, WorkflowExecution


@dataclass(frozen=True)
class WorkflowEngineState:
    workflow_id: str
    backend: str
    status: str
    active_run_id: str | None = None


class WorkflowEngine(Protocol):
    backend: str

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
        ...

    def resume_workflow(
        self,
        *,
        session: Session,
        settings: Settings,
        session_factory: sessionmaker[Session],
        workflow: WorkflowExecution,
        request: RunHumanInputRequest,
    ) -> Run:
        ...

    def query_workflow(
        self,
        *,
        workflow: WorkflowExecution,
    ) -> WorkflowEngineState:
        ...
