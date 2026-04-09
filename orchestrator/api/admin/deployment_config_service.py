from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select

from orchestrator.api.schemas import (
    ProjectDeploymentConfigRead,
    ProjectDeploymentConfigWrite,
    TenantDeploymentPlaneRead,
    TenantDeploymentPlaneWrite,
)

from orchestrator.storage.models import Project, ProjectApp, Tenant


def _coerce_dict(value: object) -> dict:
    if isinstance(value, dict):
        return dict(value)
    return {}


def tenant_deployment_plane_to_schema(tenant: Tenant) -> TenantDeploymentPlaneRead:
    return TenantDeploymentPlaneRead.model_validate(_coerce_dict(tenant.deployment_plane_config))


def normalize_tenant_deployment_plane(payload: TenantDeploymentPlaneWrite) -> dict:
    return payload.model_dump(exclude_none=True)


def project_deployment_config_to_schema(project: Project | ProjectApp) -> ProjectDeploymentConfigRead:
    return ProjectDeploymentConfigRead.model_validate(_coerce_dict(project.deployment_config))


def normalize_project_deployment_config(payload: ProjectDeploymentConfigWrite) -> dict:
    return payload.model_dump(exclude_none=True)


def get_project_default_app(*, session, tenant_id: str, project_id: str) -> ProjectApp | None:  # noqa: ANN001
    return session.execute(
        select(ProjectApp)
        .where(
            ProjectApp.tenant_id == tenant_id,
            ProjectApp.project_id == project_id,
            ProjectApp.source_path == ".",
        )
        .order_by(ProjectApp.created_at.asc())
    ).scalars().first()


def ensure_project_default_app(
    *,
    session,
    tenant_id: str,
    project: Project,
) -> ProjectApp:  # noqa: ANN001
    for pending in getattr(session, "new", ()):
        if not isinstance(pending, ProjectApp):
            continue
        if pending.tenant_id == tenant_id and pending.project_id == project.project_id and pending.source_path == ".":
            return pending

    default_app = get_project_default_app(session=session, tenant_id=tenant_id, project_id=project.project_id)
    if default_app is not None:
        return default_app

    now = datetime.now(timezone.utc)
    default_app = ProjectApp(
        app_id=str(uuid4()),
        tenant_id=tenant_id,
        project_id=project.project_id,
        name=project.name,
        slug="default",
        source_path=".",
        detection_confidence=None,
        detected_runtime=None,
        detected_language=None,
        analysis_source="compatibility_default",
        build_strategy=None,
        exposed_port=None,
        healthcheck=None,
        start_command=None,
        env_schema_json={},
        secret_schema_json={},
        deployment_config=_coerce_dict(project.deployment_config),
        status="draft",
        created_at=now,
        updated_at=now,
    )
    session.add(default_app)
    return default_app


def get_project_app(*, session, tenant_id: str, project_id: str, app_id: str) -> ProjectApp | None:  # noqa: ANN001
    return session.execute(
        select(ProjectApp).where(
            ProjectApp.tenant_id == tenant_id,
            ProjectApp.project_id == project_id,
            ProjectApp.app_id == app_id,
        )
    ).scalars().first()
