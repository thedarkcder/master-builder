from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Protocol

from sqlalchemy.orm import Session, sessionmaker

from orchestrator.core.config import Settings
from orchestrator.storage.models import Run, RunHumanInputRequest, WorkflowExecution
from orchestrator.core.workflow_operation_service import WorkflowOperationHandle
from orchestrator.storage.models import WorkflowOperation

if TYPE_CHECKING:
    from orchestrator.core.workflow_advance import WorkflowAdvanceHandler, WorkflowAdvanceOutcome, WorkflowAdvanceRequest


@dataclass(frozen=True)
class WorkflowEngineState:
    workflow_id: str
    backend: str
    status: str
    active_run_id: str | None = None


class WorkflowEngine(Protocol):
    backend: str

    def advance_workflow(
        self,
        *,
        session: Session,
        settings: Settings,
        session_factory: sessionmaker[Session] | None,
        workflow_type,
        request: "WorkflowAdvanceRequest",
        resolve_advance_handler_fn: Callable[[str], "WorkflowAdvanceHandler"] | None,
    ) -> "WorkflowAdvanceOutcome":
        ...

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

    def retry_workflow_operation(
        self,
        *,
        session: Session,
        settings: Settings,
        session_factory: sessionmaker[Session],
        workflow: WorkflowExecution,
        operation: WorkflowOperation,
    ) -> WorkflowOperationHandle:
        ...
