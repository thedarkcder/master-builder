from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from orchestrator.api.discord_response_format import build_jira_issue_url
from orchestrator.api.discord_state import (
    REQUEST_PERMISSION_LABELS,
    create_allowlist_request as _create_allowlist_request,
)
from orchestrator.api.schemas import DiscordCommandRequest, DiscordCommandResponse
from orchestrator.core.runs import RUN_STATUS_QUEUED, RUN_STATUS_RUNNING
from orchestrator.storage.models import Run, Tenant, WebhookDelivery


def _format_elapsed_seconds(*, started_at: datetime | None, created_at: datetime | None) -> int:
    anchor = started_at or created_at
    if anchor is None:
        return 0
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - anchor).total_seconds()))


def _command_help_message() -> str:
    return (
        "Commands: !help, !status, !runs [N], !run <ISSUE_KEY>, !cancel <RUN_ID>, "
        "!retry <ISSUE_KEY|RUN_ID>, !issues seed <markdown>, !issues followup <answers>, "
        "!bug <summary> [-- details], !gap <ISSUE_KEY>, !policy, !link <ISSUE_KEY>, "
        "!request <run_controls|seed_issues|all_sensitive> [reason]"
    )


def _command_policy_message() -> str:
    return (
        "Default policy: PR creation allowed; label updates allowed; Jira transitions disabled unless enabled. "
        "Use !status for queue visibility (scoped to the current project channel when mapped)."
    )


def dispatch_simple_discord_command(
    *,
    session: Session,
    tenant: Tenant,
    tenant_id: str,
    payload: DiscordCommandRequest,
    command_name: str,
    arguments: list[str],
    jira_browse_base_url: str | None,
    scoped_project_id: str | None = None,
) -> DiscordCommandResponse | None:
    if command_name == "help":
        return DiscordCommandResponse(ok=True, command=command_name, message=_command_help_message(), data=None)

    if command_name == "policy":
        return DiscordCommandResponse(ok=True, command=command_name, message=_command_policy_message(), data=None)

    if command_name == "status":
        queued_query = select(func.count(Run.run_id)).where(
            Run.tenant_id == tenant_id,
            Run.status == RUN_STATUS_QUEUED,
        )
        if scoped_project_id is not None:
            queued_query = queued_query.where(Run.project_id == scoped_project_id)
        queued_count = int(session.execute(queued_query).scalar_one())

        active_query = (
            select(Run)
            .where(
                Run.tenant_id == tenant_id,
                Run.status == RUN_STATUS_RUNNING,
            )
            .order_by(Run.started_at.asc())
        )
        if scoped_project_id is not None:
            active_query = active_query.where(Run.project_id == scoped_project_id)
        active_runs = session.execute(active_query).scalars().all()
        last_webhook_seen = session.execute(
            select(func.max(WebhookDelivery.created_at)).where(WebhookDelivery.tenant_id == tenant_id)
        ).scalar_one()
        active_payload = [
            {
                "run_id": run.run_id,
                "issue_key": run.issue_key,
                "status": run.status,
                "elapsed_seconds": _format_elapsed_seconds(
                    started_at=run.started_at,
                    created_at=run.created_at,
                ),
            }
            for run in active_runs
        ]
        message = (
            f"Tenant {'enabled' if tenant.is_enabled else 'disabled'}; "
            f"queue_depth={queued_count}; active_runs={len(active_payload)}"
        )
        return DiscordCommandResponse(
            ok=True,
            command=command_name,
            message=message,
            data={
                "webhook_last_seen": last_webhook_seen.isoformat() if last_webhook_seen else None,
                "queue_depth": queued_count,
                "active_runs": active_payload,
            },
        )

    if command_name == "runs":
        limit = 10
        if arguments:
            try:
                limit = min(max(1, int(arguments[0])), 50)
            except ValueError as exc:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid run limit") from exc

        runs_query = select(Run).where(Run.tenant_id == tenant_id)
        if scoped_project_id is not None:
            runs_query = runs_query.where(Run.project_id == scoped_project_id)
        runs_query = runs_query.order_by(Run.created_at.desc()).limit(limit)
        runs = session.execute(runs_query).scalars().all()
        run_payload = [
            {
                "run_id": run.run_id,
                "issue_key": run.issue_key,
                "status": run.status,
                "pr_url": run.pr_url,
                "created_at": run.created_at.isoformat() if run.created_at else None,
            }
            for run in runs
        ]
        return DiscordCommandResponse(
            ok=True,
            command=command_name,
            message=f"Returned {len(run_payload)} run(s)",
            data={"runs": run_payload},
        )

    if command_name == "link":
        if len(arguments) != 1:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Usage: !link <ISSUE_KEY>")
        issue_key = arguments[0].strip().upper()
        latest_pr = session.execute(
            select(Run)
            .where(
                Run.tenant_id == tenant_id,
                Run.issue_key == issue_key,
                Run.pr_url.is_not(None),
            )
            .order_by(Run.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        jira_link = build_jira_issue_url(issue_key=issue_key, browse_base_url=jira_browse_base_url)
        return DiscordCommandResponse(
            ok=True,
            command=command_name,
            message=f"Links for {issue_key}",
            data={
                "issue_key": issue_key,
                "jira_url": jira_link,
                "pr_url": latest_pr.pr_url if latest_pr else None,
            },
        )

    if command_name == "request":
        if not arguments:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Usage: !request <run_controls|seed_issues|all_sensitive> [reason]",
            )
        permission = arguments[0].strip().lower()
        reason = " ".join(arguments[1:]).strip() or None
        if permission not in REQUEST_PERMISSION_LABELS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Permission must be one of: run_controls, seed_issues, all_sensitive",
            )

        _, message = _create_allowlist_request(
            session=session,
            tenant=tenant,
            user_id=payload.user_id.strip(),
            channel_id=payload.channel_id.strip() if payload.channel_id else None,
            permissions=[permission],
            reason=reason,
        )
        suffix = f" Requested permission: {REQUEST_PERMISSION_LABELS[permission]}."
        return DiscordCommandResponse(
            ok=True,
            command=command_name,
            message=f"{message}{suffix}",
            data={"user_id": payload.user_id.strip(), "requested": True, "permission": permission},
        )

    return None
