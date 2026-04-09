from __future__ import annotations

from orchestrator.api.schemas import RunRead
from orchestrator.core.platform_team_catalog_service import platform_team_catalog_service
from orchestrator.storage.models import Run


def run_to_schema(run: Run) -> RunRead:
    return RunRead(
        run_id=run.run_id,
        workflow_id=run.workflow_id,
        attempt_number=run.attempt_number,
        parent_run_id=run.parent_run_id,
        entry_mode=run.entry_mode,
        entry_stage=run.entry_stage,
        entry_checkpoint_id=run.entry_checkpoint_id,
        tenant_id=run.tenant_id,
        project_id=run.project_id,
        issue_key=run.issue_key,
        issue_summary=run.issue_summary,
        issue_url=None,
        repo_url=run.repo_url,
        branch=run.branch,
        pr_url=run.pr_url,
        status=run.status,
        waiting_for_input=run.status == "waiting_for_input",
        pending_input_request_id=None,
        last_error=None if run.status == "succeeded" else run.last_error,
        plan=run.plan,
        team_run=platform_team_catalog_service.extract_team_run_from_plan(plan=run.plan),
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )
