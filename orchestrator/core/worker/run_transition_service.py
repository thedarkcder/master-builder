from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from orchestrator.core.workflow.execution_lifecycle import apply_execution_for_new_attempt
from orchestrator.storage.models import Run, WorkflowExecution
from orchestrator.storage.run_queue_events import notify_run_enqueued

RUN_STATUS_QUEUED = "queued"


@dataclass(frozen=True)
class RunOwnership:
    worker_service_instance_id: str
    claim_id: str

    @classmethod
    def from_expected(
        cls,
        *,
        expected_worker_service_instance_id: str | None,
        expected_claim_id: str | None,
    ) -> RunOwnership:
        owner = str(expected_worker_service_instance_id or "").strip()
        claim_id = str(expected_claim_id or "").strip()
        if not owner or not claim_id:
            raise RuntimeError("Worker-owned run transition requires worker_service_instance_id and claim_id")
        return cls(worker_service_instance_id=owner, claim_id=claim_id)


class WorkerRunTransitionService:
    def __init__(self, *, session: Session) -> None:
        self._session = session

    def require_owned_run(
        self,
        *,
        run: Run,
        ownership: RunOwnership,
        allow_statuses: set[str],
        action: str,
    ) -> Run:
        self._session.refresh(run)
        if run.status not in allow_statuses:
            raise RuntimeError(
                f"Run ownership mismatch for {action}: run {run.run_id} has status {run.status}; expected {sorted(allow_statuses)}"
            )
        current_owner = str(run.worker_service_instance_id or "").strip()
        current_claim_id = str(run.claim_id or "").strip()
        if current_owner != ownership.worker_service_instance_id or current_claim_id != ownership.claim_id:
            raise RuntimeError(
                f"Run ownership mismatch for {action}: run {run.run_id} owner={current_owner} claim_id={current_claim_id}"
            )
        return run

    @staticmethod
    def release_claim(run: Run) -> None:
        run.claim_id = None
        run.dispatch_claimed_at = None
        run.last_heartbeat_at = None
        run.worker_service_instance_id = None

    def reset_for_new_attempt(
        self,
        *,
        run: Run,
        now: datetime,
    ) -> None:
        run.status = RUN_STATUS_QUEUED
        run.last_error = None
        run.dispatch_claimed_at = None
        run.started_at = None
        run.last_heartbeat_at = None
        run.finished_at = None
        run.worker_service_instance_id = None
        run.claim_id = None
        apply_execution_for_new_attempt(
            session=self._session,
            run=run,
            latest_checkpoint_id=getattr(self._workflow_for_run(run=run), "latest_checkpoint_id", None),
            now=now,
        )
        notify_run_enqueued(
            self._session,
            tenant_id=run.tenant_id,
            project_id=run.project_id,
            run_id=run.run_id,
        )

    def _workflow_for_run(self, *, run: Run) -> WorkflowExecution | None:
        return self._session.get(WorkflowExecution, run.workflow_id)
