from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from orchestrator.api.dependencies import get_session
from orchestrator.api.schemas import RunRead
from orchestrator.core.security import require_admin
from orchestrator.storage.models import Run

router = APIRouter(tags=["runs"])


def _run_to_schema(run: Run, workflow_execution_id: str | None = None) -> RunRead:
    return RunRead(
        run_id=run.run_id,
        workflow_execution_id=workflow_execution_id or run.workflow_id,
        workflow_id=run.workflow_id,
        attempt_number=run.attempt_number,
        parent_run_id=run.parent_run_id,
        entry_mode=run.entry_mode,
        entry_stage=run.entry_stage,
        entry_checkpoint_id=run.entry_checkpoint_id,
        tenant_id=run.tenant_id,
        project_id=run.project_id,
        issue_key=run.issue_key,
        issue_summary=getattr(run, "issue_summary", None),
        issue_url=None,
        repo_url=run.repo_url,
        branch=run.branch,
        pr_url=run.pr_url,
        status=run.status,
        waiting_for_input=run.status == "waiting_for_input",
        pending_input_request_id=None,
        last_error=None if run.status == "succeeded" else run.last_error,
        plan=run.plan,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


@router.get("/runs/{run_id}", response_model=RunRead)
def get_run(
    run_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> RunRead:
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    return _run_to_schema(run)
