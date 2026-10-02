from __future__ import annotations

from sqlalchemy import select

from orchestrator.api.schemas import (
    ProjectDeploymentConfigRead,
    ProjectDeploymentConfigWrite,
    ProjectDeploymentPolicyRead,
    ProjectDeploymentPolicyWrite,
    TenantDeploymentPlaneRead,
    TenantDeploymentPlaneWrite,
)

from orchestrator.storage.models import Project, ProjectApp, Tenant


def _coerce_dict(value: object) -> dict:
    if isinstance(value, dict):
        return dict(value)
    return {}


def tenant_deployment_plane_to_schema(tenant: Tenant) -> TenantDeploymentPlaneRead:
    return TenantDeploymentPlaneRead.model_validate(
        _coerce_dict(tenant.deployment_plane_config)
    )


def normalize_tenant_deployment_plane(payload: TenantDeploymentPlaneWrite) -> dict:
    return payload.model_dump(exclude_none=True)


def project_deployment_config_to_schema(
    project: Project | ProjectApp,
) -> ProjectDeploymentConfigRead:
    return ProjectDeploymentConfigRead.model_validate(
        _coerce_dict(project.deployment_config)
    )


def normalize_project_deployment_config(payload: ProjectDeploymentConfigWrite) -> dict:
    normalized = payload.model_dump(exclude_none=True)
    normalized.pop("enabled", None)
    return normalized


def project_deployment_policy_to_schema(
    project: Project,
) -> ProjectDeploymentPolicyRead:
    return ProjectDeploymentPolicyRead.model_validate(
        _coerce_dict(project.deployment_config)
    )


def normalize_project_deployment_policy(payload: ProjectDeploymentPolicyWrite) -> dict:
    return payload.model_dump(exclude_none=True)


def get_project_default_app(
    *, session, tenant_id: str, project_id: str
) -> ProjectApp | None:  # noqa: ANN001
    return (
        session.execute(
            select(ProjectApp)
            .where(
                ProjectApp.tenant_id == tenant_id,
                ProjectApp.project_id == project_id,
                ProjectApp.source_path == ".",
            )
            .order_by(ProjectApp.created_at.asc())
        )
        .scalars()
        .first()
    )


def get_project_app(
    *, session, tenant_id: str, project_id: str, app_id: str
) -> ProjectApp | None:  # noqa: ANN001
    return (
        session.execute(
            select(ProjectApp).where(
                ProjectApp.tenant_id == tenant_id,
                ProjectApp.project_id == project_id,
                ProjectApp.app_id == app_id,
            )
        )
        .scalars()
        .first()
    )
