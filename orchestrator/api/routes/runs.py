from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from orchestrator.api.dependencies import get_session
from orchestrator.api.schemas import RunRead
from orchestrator.core.security import require_admin
from orchestrator.storage.models import Run

router = APIRouter(tags=["runs"])


def _run_to_schema(run: Run) -> RunRead:
    return RunRead(
        run_id=run.run_id,
        tenant_id=run.tenant_id,
        project_id=run.project_id,
        issue_key=run.issue_key,
        issue_summary=getattr(run, "issue_summary", None),
        issue_url=None,
        repo_url=run.repo_url,
        branch=run.branch,
        pr_url=run.pr_url,
        status=run.status,
        last_error=run.last_error,
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
