from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.storage.models import Run
from orchestrator.storage.models import Tenant


def _latest_run_diagnostics_by_issue(
    *, session: Session, tenant_id: str, issue_keys: list[str]
) -> dict[str, dict[str, str]]:
    if not issue_keys:
        return {}
    try:
        runs = session.execute(
            select(Run)
            .where(Run.tenant_id == tenant_id, Run.issue_key.in_(issue_keys))
            .order_by(Run.created_at.desc())
        ).scalars()
    except Exception:
        return {}

    latest_by_issue: dict[str, dict[str, str]] = {}
    for run in runs:
        issue_key = str(run.issue_key or "").strip().upper()
        if not issue_key or issue_key in latest_by_issue:
            continue
        if not run.last_error and str(run.status or "").strip().lower() not in {
            "failed",
            "blocked",
        }:
            continue
        latest_by_issue[issue_key] = {
            "status": str(run.status or "").strip(),
            "last_error": str(run.last_error or "").strip()[:500],
            "run_id": str(run.run_id or "").strip(),
        }
    return latest_by_issue


def collect_ask_context(
    *,
    session: Session,
    tenant: Tenant,
    channel_id: str | None,
    question: str,
    scoped_issue_key: str | None = None,
    project_filter_jql_fn,
    search_issues_fn,
) -> tuple[str | None, str | None, list[dict], dict[str, int]]:
    project_jql = project_filter_jql_fn(
        session=session, tenant=tenant, channel_id=channel_id
    )
    if scoped_issue_key:
        normalized_issue_key = scoped_issue_key.strip().upper()
        jira_issues = search_issues_fn(
            session=session,
            tenant=tenant,
            jql=f'{project_jql} AND key = "{normalized_issue_key}"',
            max_results=1,
        )
        if not jira_issues:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Issue {normalized_issue_key} was not found for this tenant",
            )
    else:
        normalized_issue_key = None

    requested_status = None

    if normalized_issue_key is None:
        jira_issues = search_issues_fn(
            session=session,
            tenant=tenant,
            jql=f"{project_jql} ORDER BY updated DESC",
            max_results=60,
        )

    issues = [
        {
            "key": issue.key,
            "summary": issue.summary,
            "status": issue.status,
        }
        for issue in jira_issues
    ]
    run_diagnostics = _latest_run_diagnostics_by_issue(
        session=session,
        tenant_id=tenant.tenant_id,
        issue_keys=[
            str(issue["key"]).strip().upper()
            for issue in issues
            if str(issue["key"]).strip()
        ],
    )
    for issue in issues:
        diagnostics = run_diagnostics.get(str(issue["key"] or "").strip().upper())
        if diagnostics:
            issue["latest_run"] = diagnostics

    status_counts: dict[str, int] = {}
    for issue in issues:
        issue_status = issue["status"]
        status_counts[issue_status] = status_counts.get(issue_status, 0) + 1

    return normalized_issue_key, requested_status, issues, status_counts
