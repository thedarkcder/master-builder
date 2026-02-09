from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.storage.models import Project
from orchestrator.tools.repo_allowlist import normalize_repo_identifier


def jira_project_key_from_issue_key(issue_key: str) -> str | None:
    normalized = str(issue_key or "").strip().upper()
    if "-" not in normalized:
        return None
    project_key = normalized.split("-", maxsplit=1)[0].strip()
    return project_key or None


def find_active_project_for_issue_key(
    session: Session,
    *,
    tenant_id: str,
    issue_key: str,
) -> Project | None:
    jira_project_key = jira_project_key_from_issue_key(issue_key)
    if not jira_project_key:
        return None
    return session.execute(
        select(Project).where(
            Project.tenant_id == tenant_id,
            Project.jira_project_key == jira_project_key,
            Project.is_archived.is_(False),
        )
    ).scalar_one_or_none()


def find_active_project_for_repo_full_name(
    session: Session,
    *,
    tenant_id: str,
    repo_full_name: str,
) -> Project | None:
    normalized_repo = normalize_repo_identifier(f"https://github.com/{repo_full_name}")
    projects = session.execute(
        select(Project).where(
            Project.tenant_id == tenant_id,
            Project.is_archived.is_(False),
        )
    ).scalars()
    for project in projects:
        if normalize_repo_identifier(project.github_repository) == normalized_repo:
            return project
    return None
