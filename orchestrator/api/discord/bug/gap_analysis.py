from __future__ import annotations

from urllib.parse import quote_plus

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.api.discord.ask.context import fetch_jira_issue_detail_for_tenant
from orchestrator.api.discord.shared.followup_format import resolve_tenant_jira_browse_base_url
from orchestrator.api.discord.shared.response_format import format_issue_markdown_link
from orchestrator.core.runs import RUN_STATUS_SUCCEEDED
from orchestrator.storage.models import Run, Tenant

GAP_HEADING_STOP_WORDS = {
    "objective",
    "scope",
    "scope in",
    "scope out",
    "how to test",
    "notes",
    "decision gate",
    "good to do",
}


def tenant_repo_url(tenant: Tenant) -> str | None:
    repo_url = str((tenant.repos_config or {}).get("github_repository") or "").strip()
    return repo_url or None


def extract_acceptance_criteria_from_description(description: str) -> list[str]:
    lines = [line.strip() for line in description.splitlines() if line.strip()]
    if not lines:
        return []

    acceptance_lines: list[str] = []
    in_acceptance_section = False
    for line in lines:
        normalized = line.lower().rstrip(":")
        if not in_acceptance_section and normalized == "acceptance criteria":
            in_acceptance_section = True
            continue
        if in_acceptance_section:
            if normalized in GAP_HEADING_STOP_WORDS:
                break
            cleaned = line.lstrip("-*• ").strip()
            if cleaned:
                acceptance_lines.append(cleaned)
            continue
        if normalized.startswith("acceptance criteria"):
            remainder = line.split(":", 1)[1].strip() if ":" in line else ""
            if remainder:
                acceptance_lines.append(remainder)
                in_acceptance_section = True

    if acceptance_lines:
        return acceptance_lines[:8]

    fallback: list[str] = []
    for line in lines:
        normalized = line.lower()
        if any(token in normalized for token in ("must", "should", "returns", "include", "supports")):
            fallback.append(line.lstrip("-*• ").strip())
    return fallback[:5]


def gap_confidence(*, has_acceptance: bool, has_successful_run: bool, has_pr: bool) -> str:
    score = int(has_acceptance) + int(has_successful_run) + int(has_pr)
    if score >= 3:
        return "high"
    if score == 2:
        return "medium"
    return "low"


def run_gap_analysis(
    *,
    session: Session,
    tenant: Tenant,
    issue_key: str,
    issue_key_pattern,
) -> tuple[str, dict]:  # noqa: ANN001
    normalized_issue_key = issue_key.strip().upper()
    if not issue_key_pattern.match(normalized_issue_key):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Usage: !gap <ISSUE_KEY>")

    issue = fetch_jira_issue_detail_for_tenant(session=session, tenant=tenant, issue_key=normalized_issue_key)
    acceptance = extract_acceptance_criteria_from_description(issue.description)
    latest_run = session.execute(
        select(Run)
        .where(Run.tenant_id == tenant.tenant_id, Run.issue_key == normalized_issue_key)
        .order_by(Run.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()

    jira_base_url = resolve_tenant_jira_browse_base_url(session=session, tenant=tenant)
    jira_url = f"{jira_base_url}/browse/{normalized_issue_key}" if jira_base_url else None
    repo_url = tenant_repo_url(tenant)
    repo_issue_search_url = f"{repo_url}/search?q={quote_plus(normalized_issue_key)}" if repo_url else None
    pr_url = str(latest_run.pr_url or "").strip() if latest_run else ""
    has_successful_run = latest_run is not None and latest_run.status == RUN_STATUS_SUCCEEDED
    has_pr = bool(pr_url)
    confidence = gap_confidence(
        has_acceptance=bool(acceptance),
        has_successful_run=has_successful_run,
        has_pr=has_pr,
    )

    gaps: list[str] = []
    if not acceptance:
        gaps.append("Acceptance criteria are missing or unclear in Jira description.")
    if latest_run is None:
        gaps.append("No run has been executed yet for this issue.")
    elif latest_run.status != RUN_STATUS_SUCCEEDED:
        gaps.append(f"Latest run `{latest_run.run_id}` is `{latest_run.status}` (not `succeeded`).")
    if not pr_url:
        gaps.append("No PR is linked to the latest run.")

    next_actions: list[str] = []
    if not acceptance:
        next_actions.append("Update Jira with explicit acceptance criteria bullets.")
    if latest_run is None or latest_run.status != RUN_STATUS_SUCCEEDED:
        next_actions.append(f"Run `!run {normalized_issue_key}` and resolve failures.")
    if not pr_url:
        next_actions.append("Create or link a PR that implements the issue scope.")
    if not next_actions:
        next_actions.append("Re-validate acceptance criteria against latest merged code before release.")

    issue_label = format_issue_markdown_link(
        issue_key=normalized_issue_key,
        browse_base_url=jira_base_url,
    )
    lines = [
        f"Gap analysis for {issue_label}: {issue.summary}",
        f"Confidence: **{confidence}**",
        "",
        "Evidence:",
    ]
    if latest_run is None:
        lines.append("- Latest run: none")
    else:
        lines.append(
            f"- Latest run: `{latest_run.run_id}` ({latest_run.status})"
            + (f" | PR: [open]({pr_url})" if pr_url else "")
        )
    if repo_issue_search_url:
        lines.append(f"- Code reference: [repo search for {normalized_issue_key}]({repo_issue_search_url})")

    lines.extend(["", "Acceptance Criteria:"])
    if acceptance:
        for criterion in acceptance[:6]:
            criterion_search = f"{repo_url}/search?q={quote_plus(criterion)}" if repo_url else None
            if criterion_search:
                lines.append(f"- {criterion} ([code search]({criterion_search}))")
            else:
                lines.append(f"- {criterion}")
    else:
        lines.append("- None detected in Jira description.")

    lines.extend(["", "Gaps:"])
    if gaps:
        lines.extend([f"- {gap}" for gap in gaps])
    else:
        lines.append("- No obvious gaps detected from Jira + latest run metadata.")

    lines.extend(["", "Next actions:"])
    lines.extend([f"- {action}" for action in next_actions[:4]])

    return (
        "\n".join(lines),
        {
            "issue_key": normalized_issue_key,
            "jira_url": jira_url,
            "pr_url": pr_url or None,
            "repo_search_url": repo_issue_search_url,
            "latest_run_id": latest_run.run_id if latest_run else None,
            "latest_run_status": latest_run.status if latest_run else None,
            "acceptance_criteria": acceptance,
            "gaps": gaps,
            "confidence": confidence,
        },
    )
