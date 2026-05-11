from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.api.admin.deployment_release_service import create_project_deployment_release
from orchestrator.api.deployment_schemas import ProjectDeploymentPolicyRead
from orchestrator.api.schemas import ProjectDeploymentReleaseCreate, ProjectDeploymentReleaseRead
from orchestrator.storage.models import Project, ProjectApp, Tenant


@dataclass(frozen=True)
class GitHubDeploymentReleaseRequest:
    branch: str
    commit_sha: str
    delivery_id: str | None


@dataclass(frozen=True)
class GitHubDeploymentReleaseResult:
    created_releases: tuple[ProjectDeploymentReleaseRead, ...]
    skipped_app_ids: tuple[str, ...]


def create_deployment_releases_for_github_push(
    *,
    session: Session,
    tenant: Tenant,
    project: Project,
    request: GitHubDeploymentReleaseRequest,
) -> GitHubDeploymentReleaseResult:
    branch = _required_string(request.branch, "branch")
    commit_sha = _required_string(request.commit_sha, "commit_sha")
    deployment_policy = ProjectDeploymentPolicyRead.model_validate(dict(project.deployment_config or {}))
    if not deployment_policy.enabled:
        return GitHubDeploymentReleaseResult(created_releases=(), skipped_app_ids=())
    if deployment_policy.production_branch != branch:
        return GitHubDeploymentReleaseResult(created_releases=(), skipped_app_ids=())
    apps = session.execute(
        select(ProjectApp)
        .where(
            ProjectApp.tenant_id == tenant.tenant_id,
            ProjectApp.project_id == project.project_id,
            ProjectApp.source_path == ".",
        )
        .order_by(ProjectApp.created_at.asc(), ProjectApp.app_id.asc())
    ).scalars().all()

    created: list[ProjectDeploymentReleaseRead] = []
    for app in apps:
        release = create_project_deployment_release(
            session=session,
            tenant_id=tenant.tenant_id,
            project_id=project.project_id,
            payload=ProjectDeploymentReleaseCreate(
                app_id=app.app_id,
                git_ref=branch,
                commit_sha=commit_sha,
                reason=f"GitHub push {request.delivery_id}" if request.delivery_id else "GitHub push",
            ),
            requested_by_user_id=None,
        )
        created.append(release)
    return GitHubDeploymentReleaseResult(
        created_releases=tuple(created),
        skipped_app_ids=(),
    )


def _required_string(value: object, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"GitHub deployment event requires {field_name}")
    return normalized
