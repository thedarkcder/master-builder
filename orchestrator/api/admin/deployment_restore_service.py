from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy import select

from orchestrator.api.admin.deployment_config_service import (
    get_project_app,
    project_deployment_config_to_schema,
    tenant_deployment_plane_to_schema,
)
from orchestrator.api.admin.deployment_release_service import (
    _coolify_api_base_url,
    _resolve_secret_value,
)
from orchestrator.api.admin.deployment_host_service import (
    resolve_active_deployment_host,
    resolve_default_active_deployment_host,
)
from orchestrator.api.schemas import (
    ProjectDeploymentBackupExecutionListRead,
    ProjectDeploymentBackupExecutionRead,
    ProjectDeploymentBackupPolicyWrite,
    ProjectDeploymentBackupRestoreRequest,
    ProjectDeploymentConfigRead,
    ProjectDeploymentResourceWrite,
    ProjectDeploymentRestoreRunRead,
    TenantDeploymentPlaneRead,
)
from orchestrator.core.config import get_settings
from orchestrator.core.deployment_host_queue import (
    DeploymentHostCommandEnqueueRequest,
    enqueue_deployment_host_command,
)
from orchestrator.core.deployment_restore_executor import (
    DeploymentRestoreExecutionContext,
)
from orchestrator.storage.models import (
    Project,
    ProjectApp,
    ProjectDeploymentRestoreRun,
    Tenant,
)
from orchestrator.tools.coolify_api import CoolifyApiClient, CoolifyApiConfig

_ACTIVE_DEPLOYMENT_PLANE_STATES = {"active", "degraded"}
_SUPPORTED_RESTORE_DATABASE_TYPES = {"postgres", "mysql", "mariadb"}
_TERMINAL_RESTORE_STATUSES = {"succeeded", "failed"}


def _coerce_dict(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return dict(value)
    return {}


def _normalize_optional_string(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _project_app_context(
    *,
    session,
    tenant_id: str,
    project_id: str,
    app_id: str,
) -> tuple[
    Tenant, Project, ProjectApp, TenantDeploymentPlaneRead, ProjectDeploymentConfigRead
]:  # noqa: ANN001
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found"
        )
    project = session.get(Project, project_id)
    if project is None or project.tenant_id != tenant_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        )
    app = get_project_app(
        session=session, tenant_id=tenant_id, project_id=project_id, app_id=app_id
    )
    if app is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project app not found"
        )
    tenant_plane = tenant_deployment_plane_to_schema(tenant)
    return tenant, project, app, tenant_plane, project_deployment_config_to_schema(app)


def _build_internal_coolify_client(
    *,
    session,
    tenant: Tenant,
    tenant_plane: TenantDeploymentPlaneRead,
    project: Project,
) -> CoolifyApiClient:
    if tenant_plane.provider != "internal_coolify":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Tenant deployment plane is not configured for internal Coolify",
        )
    if tenant_plane.state not in _ACTIVE_DEPLOYMENT_PLANE_STATES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Tenant deployment plane is not active",
        )

    api_token_ref = _normalize_optional_string(
        (tenant_plane.secret_refs or {}).get("coolify_api_token")
    )
    if api_token_ref is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Tenant deployment plane is missing coolify_api_token secret ref",
        )
    settings = get_settings()
    api_token = _resolve_secret_value(
        session=session,
        secret_ref=api_token_ref,
        tenant_id=tenant.tenant_id,
        project_id=project.project_id,
        encryption_key=settings.secrets_encryption_key,
    )
    if api_token is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Tenant deployment plane references missing Coolify API token",
        )
    return CoolifyApiClient(
        CoolifyApiConfig(
            base_url=_coolify_api_base_url(tenant_plane=tenant_plane),
            bearer_token=api_token,
        )
    )


def _resource_database_type(resource: ProjectDeploymentResourceWrite) -> str | None:
    kind = str(resource.kind or "").strip().lower()
    if kind in _SUPPORTED_RESTORE_DATABASE_TYPES:
        return kind
    if kind == "database":
        config = _coerce_dict(resource.config)
        return (
            _normalize_optional_string(
                config.get("database_type") or config.get("coolify_database_type")
            )
            or "postgres"
        )
    return None


def _selected_backup_policy(
    config: ProjectDeploymentConfigRead, backup_key: str
) -> ProjectDeploymentBackupPolicyWrite:
    normalized_key = str(backup_key or "").strip().lower()
    for policy in config.backup_policies:
        if str(policy.key or "").strip().lower() == normalized_key:
            return policy
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Backup policy '{backup_key}' was not found",
    )


def _selected_resource(
    config: ProjectDeploymentConfigRead, resource_key: str
) -> ProjectDeploymentResourceWrite:
    normalized_key = str(resource_key or "").strip().lower()
    for resource in config.resources:
        if str(resource.key or "").strip().lower() == normalized_key:
            return resource
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Resource '{resource_key}' was not found",
    )


def _database_uuid(resource: ProjectDeploymentResourceWrite) -> str:
    database_uuid = _normalize_optional_string(
        _coerce_dict(resource.config).get("coolify_uuid")
    )
    if database_uuid is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Resource '{resource.key}' has not been applied to Coolify yet",
        )
    return database_uuid


def _backup_uuid(
    policy: ProjectDeploymentBackupPolicyWrite,
    payload: ProjectDeploymentBackupRestoreRequest,
) -> str:
    backup_uuid = _normalize_optional_string(
        payload.backup_uuid
    ) or _normalize_optional_string(
        _coerce_dict(policy.config).get("coolify_backup_uuid")
    )
    if backup_uuid is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Backup policy '{policy.key}' has not been applied to Coolify yet",
        )
    return backup_uuid


def _normalize_execution(
    payload: dict[str, object],
) -> ProjectDeploymentBackupExecutionRead | None:
    execution_uuid = (
        _normalize_optional_string(payload.get("execution_uuid"))
        or _normalize_optional_string(payload.get("uuid"))
        or _normalize_optional_string(payload.get("id"))
    )
    if execution_uuid is None:
        return None
    artifact_path = (
        _normalize_optional_string(payload.get("artifact_path"))
        or _normalize_optional_string(payload.get("path"))
        or _normalize_optional_string(payload.get("file_path"))
        or _normalize_optional_string(payload.get("full_path"))
        or _normalize_optional_string(payload.get("absolute_path"))
    )
    file_name = _normalize_optional_string(
        payload.get("file_name")
    ) or _normalize_optional_string(payload.get("filename"))
    return ProjectDeploymentBackupExecutionRead(
        execution_uuid=execution_uuid,
        status=_normalize_optional_string(payload.get("status")),
        created_at=payload.get("created_at"),
        started_at=payload.get("started_at"),
        completed_at=payload.get("completed_at"),
        artifact_path=artifact_path,
        file_name=file_name,
        details=dict(payload),
    )


def _list_execution_rows(
    *,
    client: CoolifyApiClient,
    database_uuid: str,
    backup_uuid: str,
) -> list[ProjectDeploymentBackupExecutionRead]:
    rows: list[ProjectDeploymentBackupExecutionRead] = []
    for execution in client.list_database_backup_executions(
        database_uuid=database_uuid, backup_uuid=backup_uuid
    ):
        normalized = _normalize_execution(_coerce_dict(execution))
        if normalized is not None:
            rows.append(normalized)
    return rows


def list_project_deployment_backup_executions(
    *,
    session,
    tenant_id: str,
    project_id: str,
    app_id: str,
    backup_key: str,
) -> ProjectDeploymentBackupExecutionListRead:  # noqa: ANN001
    tenant, project, _app, tenant_plane, deployment_config = _project_app_context(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        app_id=app_id,
    )
    backup_policy = _selected_backup_policy(deployment_config, backup_key)
    resource = _selected_resource(deployment_config, backup_policy.resource_key)
    database_type = _resource_database_type(resource)
    if database_type not in _SUPPORTED_RESTORE_DATABASE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Resource '{resource.key}' is not restorable in v1",
        )
    database_uuid = _database_uuid(resource)
    backup_uuid = _normalize_optional_string(
        _coerce_dict(backup_policy.config).get("coolify_backup_uuid")
    )
    if backup_uuid is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Backup policy '{backup_policy.key}' has not been applied to Coolify yet",
        )
    client = _build_internal_coolify_client(
        session=session, tenant=tenant, tenant_plane=tenant_plane, project=project
    )
    return ProjectDeploymentBackupExecutionListRead(
        backup_key=backup_policy.key,
        resource_key=resource.key,
        backup_uuid=backup_uuid,
        database_uuid=database_uuid,
        executions=_list_execution_rows(
            client=client, database_uuid=database_uuid, backup_uuid=backup_uuid
        ),
    )


def _restore_run_to_schema(
    run: ProjectDeploymentRestoreRun,
) -> ProjectDeploymentRestoreRunRead:
    return ProjectDeploymentRestoreRunRead(
        restore_run_id=run.restore_run_id,
        tenant_id=run.tenant_id,
        project_id=run.project_id,
        app_id=run.app_id,
        host_id=run.host_id,
        command_id=run.command_id,
        backup_policy_key=run.backup_policy_key,
        resource_key=run.resource_key,
        backup_uuid=run.backup_uuid,
        execution_uuid=run.execution_uuid,
        database_type=run.database_type,  # type: ignore[arg-type]
        database_uuid=run.database_uuid,
        restore_mode=run.restore_mode,  # type: ignore[arg-type]
        requested_by_user_id=run.requested_by_user_id,
        confirmation_value=run.confirmation_value,
        execution_payload=_coerce_dict(run.execution_payload),
        status=run.status,  # type: ignore[arg-type]
        last_error=run.last_error,
        created_at=run.created_at,
        started_at=run.started_at,
        completed_at=run.completed_at,
        updated_at=run.updated_at,
    )


def list_project_deployment_restore_runs(
    *,
    session,
    tenant_id: str,
    project_id: str,
    app_id: str,
) -> list[ProjectDeploymentRestoreRunRead]:  # noqa: ANN001
    _project_app_context(
        session=session, tenant_id=tenant_id, project_id=project_id, app_id=app_id
    )
    runs = (
        session.execute(
            select(ProjectDeploymentRestoreRun)
            .where(
                ProjectDeploymentRestoreRun.tenant_id == tenant_id,
                ProjectDeploymentRestoreRun.project_id == project_id,
                ProjectDeploymentRestoreRun.app_id == app_id,
            )
            .order_by(ProjectDeploymentRestoreRun.created_at.desc())
        )
        .scalars()
        .all()
    )
    return [_restore_run_to_schema(run) for run in runs]


def get_project_deployment_restore_run(
    *,
    session,
    tenant_id: str,
    project_id: str,
    app_id: str,
    restore_run_id: str,
) -> ProjectDeploymentRestoreRunRead:  # noqa: ANN001
    _project_app_context(
        session=session, tenant_id=tenant_id, project_id=project_id, app_id=app_id
    )
    run = session.get(ProjectDeploymentRestoreRun, restore_run_id)
    if (
        run is None
        or run.tenant_id != tenant_id
        or run.project_id != project_id
        or run.app_id != app_id
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Deployment restore run not found",
        )
    return _restore_run_to_schema(run)


def _resolve_restore_host_id(
    *, session, tenant_plane: TenantDeploymentPlaneRead
) -> str:  # noqa: ANN001
    managed_host_id = _normalize_optional_string(tenant_plane.managed_host_id)
    if managed_host_id is None:
        host = resolve_default_active_deployment_host(
            session=session,
            infrastructure_provider=tenant_plane.infrastructure_provider,
            region=tenant_plane.region,
        )
    else:
        host = resolve_active_deployment_host(session=session, host_id=managed_host_id)
    capabilities = {
        str(value or "").strip().lower()
        for value in list(host.capability_keys_json or [])
    }
    if "restore_database" not in capabilities:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Managed host '{host.label}' does not support database restore",
        )
    return host.host_id


def create_project_deployment_restore_run(
    *,
    session,
    tenant_id: str,
    project_id: str,
    app_id: str,
    payload: ProjectDeploymentBackupRestoreRequest,
    requested_by_user_id: str | None,
) -> ProjectDeploymentRestoreRunRead:  # noqa: ANN001
    tenant, project, app, tenant_plane, deployment_config = _project_app_context(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        app_id=app_id,
    )
    if payload.confirmation_value != app.slug:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Restore confirmation value must match the app slug",
        )

    backup_policy = _selected_backup_policy(deployment_config, payload.backup_key)
    resource = _selected_resource(deployment_config, payload.resource_key)
    if resource.key != backup_policy.resource_key:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Backup policy '{backup_policy.key}' does not target resource '{resource.key}'",
        )
    database_type = _resource_database_type(resource)
    if database_type not in _SUPPORTED_RESTORE_DATABASE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Resource '{resource.key}' is not restorable in v1",
        )

    host_id = _resolve_restore_host_id(session=session, tenant_plane=tenant_plane)
    database_uuid = _database_uuid(resource)
    backup_uuid = _backup_uuid(backup_policy, payload)
    client = _build_internal_coolify_client(
        session=session, tenant=tenant, tenant_plane=tenant_plane, project=project
    )
    executions = _list_execution_rows(
        client=client, database_uuid=database_uuid, backup_uuid=backup_uuid
    )
    selected_execution = next(
        (
            execution
            for execution in executions
            if execution.execution_uuid == payload.execution_uuid
        ),
        None,
    )
    if selected_execution is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Backup execution was not found",
        )

    now = _now()
    run = ProjectDeploymentRestoreRun(
        restore_run_id=str(uuid4()),
        tenant_id=tenant_id,
        project_id=project_id,
        app_id=app_id,
        host_id=host_id,
        command_id=None,
        backup_policy_key=backup_policy.key,
        resource_key=resource.key,
        backup_uuid=backup_uuid,
        execution_uuid=selected_execution.execution_uuid,
        database_type=database_type,
        database_uuid=database_uuid,
        restore_mode="replace",
        requested_by_user_id=_normalize_optional_string(requested_by_user_id),
        confirmation_value=payload.confirmation_value,
        execution_payload=selected_execution.model_dump(mode="json"),
        status="queued",
        last_error=None,
        created_at=now,
        started_at=None,
        completed_at=None,
        updated_at=now,
    )
    session.add(run)
    session.flush()
    command = enqueue_deployment_host_command(
        session,
        request=DeploymentHostCommandEnqueueRequest(
            host_id=host_id,
            kind="restore_database",
            tenant_id=tenant_id,
            project_id=project_id,
            app_id=app_id,
            restore_run_id=run.restore_run_id,
            release_id=None,
            payload_json={"restore_run_id": run.restore_run_id},
        ),
    )
    run.command_id = command.command_id
    session.commit()
    session.refresh(run)
    return _restore_run_to_schema(run)


def _find_restore_run(
    session, restore_run_id: str
) -> ProjectDeploymentRestoreRun | None:  # noqa: ANN001
    return session.get(ProjectDeploymentRestoreRun, restore_run_id)


def _mark_restore_run_failed(
    *,
    session,
    run: ProjectDeploymentRestoreRun,
    message: str,
) -> ProjectDeploymentRestoreRunRead:  # noqa: ANN001
    now = _now()
    run.status = "failed"
    run.last_error = message
    run.updated_at = now
    if run.started_at is None:
        run.started_at = now
    run.completed_at = now
    session.commit()
    session.refresh(run)
    return _restore_run_to_schema(run)


def _mark_restore_run_running(
    *,
    session,
    run: ProjectDeploymentRestoreRun,
) -> ProjectDeploymentRestoreRunRead:  # noqa: ANN001
    if run.status == "running":
        return _restore_run_to_schema(run)
    if run.status in _TERMINAL_RESTORE_STATUSES:
        raise RuntimeError(
            f"Deployment restore run cannot transition from status '{run.status}'"
        )
    now = _now()
    run.status = "running"
    run.started_at = run.started_at or now
    run.updated_at = now
    session.commit()
    session.refresh(run)
    return _restore_run_to_schema(run)


def _mark_restore_run_succeeded(
    *,
    session,
    run: ProjectDeploymentRestoreRun,
) -> ProjectDeploymentRestoreRunRead:  # noqa: ANN001
    now = _now()
    run.status = "succeeded"
    run.last_error = None
    run.started_at = run.started_at or now
    run.completed_at = now
    run.updated_at = now
    session.commit()
    session.refresh(run)
    return _restore_run_to_schema(run)


def _execution_context_for_run(
    *,
    session,
    resource: ProjectDeploymentResourceWrite,
    run: ProjectDeploymentRestoreRun,
) -> DeploymentRestoreExecutionContext:
    config = _coerce_dict(resource.config)
    artifact_payload = _coerce_dict(run.execution_payload)
    artifact_path = _normalize_optional_string(
        artifact_payload.get("artifact_path")
    ) or _normalize_optional_string(artifact_payload.get("path"))
    if artifact_path is None:
        raise RuntimeError("Selected backup execution does not expose an artifact path")

    username = (
        _normalize_optional_string(config.get("postgres_user"))
        or _normalize_optional_string(config.get("mysql_user"))
        or _normalize_optional_string(config.get("mariadb_user"))
        or _normalize_optional_string(config.get("user"))
    )
    password = _resolve_resource_password(session=session, run=run, config=config)
    database_name = (
        _normalize_optional_string(config.get("postgres_db"))
        or _normalize_optional_string(config.get("mysql_database"))
        or _normalize_optional_string(config.get("mariadb_database"))
        or _normalize_optional_string(config.get("database"))
    )
    port = (
        config.get("postgres_port")
        or config.get("mysql_port")
        or config.get("mariadb_port")
        or config.get("port")
        or (5432 if run.database_type == "postgres" else 3306)
    )
    if username is None or password is None or database_name is None:
        raise RuntimeError(
            f"Resource '{resource.key}' is missing database restore credentials"
        )
    return DeploymentRestoreExecutionContext(
        database_type=run.database_type,
        container_name=_normalize_optional_string(config.get("coolify_container_name"))
        or resource.name
        or resource.key,
        database_name=database_name,
        username=username,
        password=password,
        host="127.0.0.1",
        port=int(port),
        artifact_path=artifact_path,
    )


def _resolve_resource_password(
    *,
    session,
    run: ProjectDeploymentRestoreRun,
    config: dict[str, object],
) -> str | None:  # noqa: ANN001
    settings = get_settings()
    encryption_key = str(getattr(settings, "secrets_encryption_key", "") or "").strip()
    for key in (
        "postgres_password",
        "mysql_password",
        "mariadb_password",
        "password",
    ):
        secret_ref = _normalize_optional_string(config.get(f"{key}_secret_ref"))
        if secret_ref is None:
            continue
        resolved = _resolve_secret_value(
            session=session,
            secret_ref=secret_ref,
            tenant_id=run.tenant_id,
            project_id=run.project_id,
            encryption_key=encryption_key,
        )
        if resolved is None:
            raise RuntimeError(
                f"Resource '{run.resource_key}' references missing secret for {key}"
            )
        return resolved
    return None


def _container_candidates_for_restore(
    *,
    resource: ProjectDeploymentResourceWrite,
    database_uuid: str,
) -> list[str]:
    candidate_values = [
        _normalize_optional_string(
            _coerce_dict(resource.config).get("coolify_container_name")
        ),
        _normalize_optional_string(_coerce_dict(resource.config).get("container_name")),
        _normalize_optional_string(resource.name),
        _normalize_optional_string(resource.key),
        _normalize_optional_string(database_uuid),
    ]
    candidates = [value for value in candidate_values if value]
    if not candidates:
        raise RuntimeError(
            f"Resource '{resource.key}' does not expose any restore container identifiers"
        )
    return candidates


def build_restore_host_command_payload(
    *,
    session,
    restore_run_id: str,
) -> dict[str, object]:  # noqa: ANN001
    run = _find_restore_run(session, restore_run_id)
    if run is None:
        raise RuntimeError(f"Deployment restore run '{restore_run_id}' was not found")
    _tenant, _project, _app, _tenant_plane, deployment_config = _project_app_context(
        session=session,
        tenant_id=run.tenant_id,
        project_id=run.project_id,
        app_id=run.app_id,
    )
    backup_policy = _selected_backup_policy(deployment_config, run.backup_policy_key)
    resource = _selected_resource(deployment_config, run.resource_key)
    if resource.key != backup_policy.resource_key:
        raise RuntimeError(
            f"Backup policy '{backup_policy.key}' no longer targets resource '{resource.key}'"
        )
    execution_context = _execution_context_for_run(
        session=session, resource=resource, run=run
    )
    return {
        "restore_run_id": run.restore_run_id,
        "database_type": run.database_type,
        "database_uuid": run.database_uuid,
        "resource_key": resource.key,
        "execution_context": {
            "database_type": execution_context.database_type,
            "container_name": execution_context.container_name,
            "database_name": execution_context.database_name,
            "username": execution_context.username,
            "password": execution_context.password,
            "host": execution_context.host,
            "port": execution_context.port,
            "artifact_path": execution_context.artifact_path,
        },
        "container_candidates": _container_candidates_for_restore(
            resource=resource, database_uuid=run.database_uuid
        ),
    }


def start_project_deployment_restore_run(
    *,
    session,
    restore_run_id: str,
) -> ProjectDeploymentRestoreRunRead:  # noqa: ANN001
    run = _find_restore_run(session, restore_run_id)
    if run is None:
        raise RuntimeError(f"Deployment restore run '{restore_run_id}' was not found")
    return _mark_restore_run_running(session=session, run=run)


def complete_project_deployment_restore_run(
    *,
    session,
    restore_run_id: str,
    status: str,
    last_error: str | None = None,
) -> ProjectDeploymentRestoreRunRead:  # noqa: ANN001
    run = _find_restore_run(session, restore_run_id)
    if run is None:
        raise RuntimeError(f"Deployment restore run '{restore_run_id}' was not found")
    normalized_status = str(status or "").strip().lower()
    if normalized_status == "succeeded":
        return _mark_restore_run_succeeded(session=session, run=run)
    if normalized_status == "failed":
        return _mark_restore_run_failed(
            session=session,
            run=run,
            message=_normalize_optional_string(last_error)
            or "Deployment host restore failed",
        )
    raise RuntimeError("Deployment restore run status is invalid")
