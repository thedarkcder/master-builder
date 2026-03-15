from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.storage.models import Project, Tenant
from orchestrator.tools.project_repo_checkout import project_repo_dir


def resolve_codex_working_dir(
    *,
    session: Session,
    tenant: Tenant,
    settings,  # noqa: ANN001
    project_id: str | None = None,
    project_keys: list[str] | tuple[str, ...] | None = None,
) -> str:
    project = _resolve_project(
        session=session,
        tenant=tenant,
        project_id=project_id,
        project_keys=project_keys,
    )
    if project is not None:
        repo_dir = project_repo_dir(
            base_dir=settings.project_repo_checkout_base_dir,
            tenant_id=tenant.tenant_id,
            project_id=project.project_id,
        )
        if repo_dir.is_dir():
            return str(repo_dir)
    return str(Path.cwd())


def _resolve_project(
    *,
    session: Session,
    tenant: Tenant,
    project_id: str | None,
    project_keys: list[str] | tuple[str, ...] | None,
) -> Project | None:
    if project_id:
        project = session.get(Project, project_id)
        if (
            project is not None
            and project.tenant_id == tenant.tenant_id
            and not bool(project.is_archived)
        ):
            return project

    normalized_project_keys = {
        str(key).strip().upper() for key in (project_keys or []) if str(key).strip()
    }
    if len(normalized_project_keys) != 1:
        return None
    project_key = next(iter(normalized_project_keys))
    return session.execute(
        select(Project).where(
            Project.tenant_id == tenant.tenant_id,
            Project.is_archived.is_(False),
            Project.jira_project_key == project_key,
        )
    ).scalar_one_or_none()
