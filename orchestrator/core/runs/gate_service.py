from __future__ import annotations

from sqlalchemy.orm import Session

from orchestrator.core.runs.service import (
    EnqueueRunResult,
    enqueue_run,
    resolve_precheck_outcome_for_enqueue,
)


def enqueue_issue_run_with_precheck(
    session: Session,
    *,
    tenant_id: str,
    project_id: str | None,
    issue_key: str,
    issue_summary: str | None,
    issue_description: str | None,
    repo_url: str | None,
    delivery_id: str | None,
    precheck_outcome: str | None,
    required_worker_capability: str | None,
    max_concurrent_runs: int | None,
) -> EnqueueRunResult:
    return enqueue_run(
        session,
        tenant_id=tenant_id,
        project_id=project_id,
        issue_key=issue_key,
        issue_summary=issue_summary,
        issue_description=issue_description,
        repo_url=repo_url,
        delivery_id=delivery_id,
        precheck_outcome=resolve_precheck_outcome_for_enqueue(
            precheck_outcome=precheck_outcome
        ),
        required_worker_capability=required_worker_capability,
        max_concurrent_runs=max_concurrent_runs,
    )
