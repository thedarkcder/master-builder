from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.api.schemas import DiscordCommandRequest, DiscordCommandResponse
from orchestrator.core.communications.command_pipeline import CommandScope
from orchestrator.core.communications.enqueue_reason_contract import (
    format_enqueue_conflict_detail,
)
from orchestrator.core.project_policy import resolve_effective_policy
from orchestrator.core.runs import cancel_run, enqueue_run
from orchestrator.storage.models import Run, Tenant


def dispatch_run_control_command(
    *,
    session: Session,
    tenant: Tenant,
    tenant_id: str,
    payload: DiscordCommandRequest,
    command_name: str,
    arguments: list[str],
    scope: CommandScope,
    retryable_statuses: set[str],
    resolve_project_for_issue: Callable[..., Any],
    fetch_issue_preview: Callable[..., Any],
    ensure_issue_is_executable: Callable[..., Any],
) -> DiscordCommandResponse | None:
    if command_name == "run":
        if len(arguments) != 1:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Usage: !run <ISSUE_KEY>")
        issue_key = arguments[0].strip().upper()
        project = resolve_project_for_issue(
            session=session,
            tenant=tenant,
            issue_key=issue_key,
        )
        if scope.project_keys and project.jira_project_key not in set(scope.project_keys):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Issue {issue_key} is outside the mapped project scope",
            )
        issue_preview = fetch_issue_preview(session=session, tenant=tenant, issue_key=issue_key)
        ensure_issue_is_executable(issue_status=issue_preview.status, tenant=tenant)
        enqueue_result = enqueue_run(
            session,
            tenant_id=tenant_id,
            project_id=project.project_id,
            issue_key=issue_key,
            issue_summary=issue_preview.summary,
            issue_description=None,
            repo_url=project.github_repository,
            delivery_id=None,
            max_concurrent_runs=resolve_effective_policy(
                tenant_policy=tenant.policy_config,
                project_overrides=project.policy_overrides,
            ).get("max_concurrent_runs"),
        )
        if not enqueue_result.enqueued:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=format_enqueue_conflict_detail(
                    prefix="Run could not be queued",
                    enqueue_reason=str(enqueue_result.reason),
                    enqueue_run_obj=enqueue_result.run,
                ),
            )
        return DiscordCommandResponse(
            ok=True,
            command=command_name,
            message=f"Queued run {enqueue_result.run.run_id} for {issue_key}",
            data={"run_id": enqueue_result.run.run_id, "issue_key": issue_key},
        )

    if command_name == "cancel":
        if len(arguments) != 1:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Usage: !cancel <RUN_ID>")
        run_id = arguments[0].strip()
        run = session.get(Run, run_id)
        if run is None or run.tenant_id != tenant_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Run {run_id} was not found")
        if scope.project_id and run.project_id != scope.project_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Run {run_id} is outside the mapped project scope",
            )
        cancelled = cancel_run(session, run_id=run_id, cancelled_by=payload.user_id)
        return DiscordCommandResponse(
            ok=True,
            command=command_name,
            message=f"Cancelled run {cancelled.run_id}",
            data={"run_id": cancelled.run_id, "status": cancelled.status},
        )

    if command_name == "retry":
        if len(arguments) != 1:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Usage: !retry <ISSUE_KEY|RUN_ID>")
        target = arguments[0].strip()
        run = session.get(Run, target)
        if run is None:
            issue_key = target.upper()
            run = session.execute(
                select(Run)
                .where(Run.tenant_id == tenant_id, Run.issue_key == issue_key)
                .order_by(Run.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()
        if run is None or run.tenant_id != tenant_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No run was found for '{target}'",
            )
        if scope.project_id and run.project_id != scope.project_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Run {run.run_id} is outside the mapped project scope",
            )
        if run.status not in retryable_statuses:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Run {run.run_id} is {run.status}; only failed/blocked/cancelled runs can be retried",
            )
        issue_preview = fetch_issue_preview(session=session, tenant=tenant, issue_key=run.issue_key)
        ensure_issue_is_executable(issue_status=issue_preview.status, tenant=tenant)
        project = resolve_project_for_issue(
            session=session,
            tenant=tenant,
            issue_key=run.issue_key,
        )
        if scope.project_keys and project.jira_project_key not in set(scope.project_keys):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Issue {run.issue_key} is outside the mapped project scope",
            )
        enqueue_result = enqueue_run(
            session,
            tenant_id=tenant_id,
            project_id=project.project_id,
            issue_key=run.issue_key,
            issue_summary=issue_preview.summary,
            issue_description=run.issue_description,
            repo_url=project.github_repository,
            delivery_id=None,
            max_concurrent_runs=resolve_effective_policy(
                tenant_policy=tenant.policy_config,
                project_overrides=project.policy_overrides,
            ).get("max_concurrent_runs"),
        )
        if not enqueue_result.enqueued:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=format_enqueue_conflict_detail(
                    prefix="Retry could not be queued",
                    enqueue_reason=str(enqueue_result.reason),
                    enqueue_run_obj=enqueue_result.run,
                ),
            )
        return DiscordCommandResponse(
            ok=True,
            command=command_name,
            message=f"Queued retry run {enqueue_result.run.run_id} for {run.issue_key}",
            data={"run_id": enqueue_result.run.run_id, "issue_key": run.issue_key},
        )

    return None
