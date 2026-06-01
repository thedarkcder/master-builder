from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from orchestrator.api.admin.deployment_config_service import (
    get_project_default_app,
    get_project_app,
    normalize_project_deployment_config,
    normalize_project_deployment_policy,
    project_deployment_config_to_schema,
    project_deployment_policy_to_schema,
    tenant_deployment_plane_to_schema,
)
from orchestrator.api.admin.tenant_project_helpers import allocate_project_id
from orchestrator.api.admin.deployment_restore_service import (
    create_project_deployment_restore_run,
    get_project_deployment_restore_run,
    list_project_deployment_backup_executions,
    list_project_deployment_restore_runs,
)
from orchestrator.api.admin.deployment_release_service import (
    _coolify_api_base_url,
    _resolve_secret_value,
    create_project_deployment_release,
    get_project_deployment_release,
    list_project_deployment_releases,
    update_project_deployment_release_status,
)
from orchestrator.api.schemas import (
    ProjectAppAnalysisRunRead,
    ProjectAppAnalysisRunStart,
    ProjectAppCreate,
    ProjectAppRead,
    ProjectAppUpdate,
    ProjectDeploymentBackupApplyRequest,
    ProjectDeploymentBackupExecutionListRead,
    ProjectDeploymentBackupPolicyWrite,
    ProjectDeploymentBackupRestoreRequest,
    ProjectDeploymentBackupTriggerRequest,
    ProjectDeploymentConfigRead,
    ProjectDeploymentDomainApplyRequest,
    ProjectDeploymentOperationItemRead,
    ProjectDeploymentOperationRead,
    ProjectDeploymentPolicyWrite,
    ProjectDeploymentReleaseCreate,
    ProjectDeploymentSetupStartRead,
    ProjectDeploymentResourceApplyRequest,
    ProjectDeploymentResourceWrite,
    ProjectDeploymentRestoreRunRead,
    ProjectDeploymentVolumeApplyRequest,
    ProjectDeploymentVolumeWrite,
    ProjectNavigationRead,
    TenantDeploymentPlaneRead,
    TenantDeploymentsOverviewAppRead,
    TenantDeploymentsOverviewFailureRead,
    TenantDeploymentsOverviewRead,
    TenantDeploymentsOverviewSummaryRead,
)
from orchestrator.core.config import get_settings
from orchestrator.core.deployment_setup.start import (
    project_deployment_setup_execution_key,
    start_project_deployment_setup_workflow,
)
from orchestrator.core.platform.secret_manager import normalize_secret_ref
from orchestrator.core.platform.tenant_secret_service import tenant_secret_service
from orchestrator.core.webhooks.job_queue import (
    WEBHOOK_TRANSPORT_PROJECT_APP_ANALYSIS,
    WebhookJobEnqueueRequest,
    enqueue_webhook_job,
)
from orchestrator.storage.models import DeploymentHostCommand, Project, ProjectApp, ProjectAppAnalysisRun, ProjectDeploymentRelease, Tenant
from orchestrator.storage.run_queue_events import notify_webhook_job_enqueued
from orchestrator.tools.coolify_api import CoolifyApiClient, CoolifyApiConfig, CoolifyApiError
from orchestrator.tools.discord_api import DiscordApiError
from orchestrator.tools.atlassian_oauth import AtlassianOAuthError
from orchestrator.tools.project_repo_checkout import ProjectRepoCheckoutError
from orchestrator.tools.project_repo_checkout import project_repo_dir

_ACTIVE_DEPLOYMENT_PLANE_STATES = {"active", "degraded"}
_INTERNAL_COOLIFY_PROVIDER = "internal_coolify"
_DATABASE_RESOURCE_KINDS = {
    "postgres",
    "mysql",
    "mariadb",
    "mongodb",
    "redis",
    "keydb",
    "clickhouse",
    "dragonfly",
}
_SERVICE_RESOURCE_KINDS = {"one_click_service", "object_storage", "s3", "minio_s3"}
_UNSUPPORTED_RESOURCE_KINDS: set[str] = set()

class AdminProjectService:
    def __init__(
        self,
        *,
        normalize_project_repo: Callable[[str], str],
        normalize_project_key: Callable[[str], str],
        normalize_project_policy_overrides: Callable[[dict | None], dict],
        normalize_string_map: Callable[[dict | None], dict],
        normalize_project_architecture_docs_config: Callable[[dict | None], dict],
        normalize_project_discord_config: Callable[[dict | None], dict],
        with_preserved_discord_system_fields: Callable[[dict, dict], dict],
        resolve_project_discord_channel_binding: Callable[..., dict],
        sync_tenant_jira_project_keys: Callable[..., None],
        ensure_project_repository_checkout: Callable[..., None],
        resolve_project_run_board_id: Callable[..., int | None],
        project_to_schema: Callable[..., object],
        settings_factory: Callable[[], object],
    ) -> None:
        self._normalize_project_repo = normalize_project_repo
        self._normalize_project_key = normalize_project_key
        self._normalize_project_policy_overrides = normalize_project_policy_overrides
        self._normalize_string_map = normalize_string_map
        self._normalize_project_architecture_docs_config = normalize_project_architecture_docs_config
        self._normalize_project_discord_config = normalize_project_discord_config
        self._with_preserved_discord_system_fields = with_preserved_discord_system_fields
        self._resolve_project_discord_channel_binding = resolve_project_discord_channel_binding
        self._sync_tenant_jira_project_keys = sync_tenant_jira_project_keys
        self._ensure_project_repository_checkout = ensure_project_repository_checkout
        self._resolve_project_run_board_id = resolve_project_run_board_id
        self._project_to_schema = project_to_schema
        self._settings_factory = settings_factory

    def _optional_payload_dict(self, value) -> dict | None:  # noqa: ANN001
        if value is None:
            return None
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            dumped = model_dump(exclude_unset=True)
            return dumped if isinstance(dumped, dict) else dict(dumped or {})
        if isinstance(value, dict):
            return dict(value)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Structured project configuration payload must be an object",
        )

    def _should_bind_project_discord_channel(
        self,
        *,
        tenant,
        discord_config: dict,
        is_archived: bool,
        force_bind: bool = False,
    ) -> bool:  # noqa: ANN001
        if is_archived:
            return False
        existing_channel_id = str((discord_config or {}).get("channel_id") or "").strip()
        if existing_channel_id:
            return False
        if force_bind:
            return True
        tenant_discord_config = dict(getattr(tenant, "discord_config", None) or {})
        return bool(str(tenant_discord_config.get("guild_id") or "").strip())

    def _project_secret_ref(self, *, tenant_id: str, project_id: str, secret_key: str) -> str:
        normalized_key = normalize_secret_ref(secret_key)
        if normalized_key.startswith(("platform/", "tenant/", "project/")):
            raise ValueError("Project secret variable names must not include a scope prefix")
        return f"project/{tenant_id}/{project_id}/{normalized_key}"

    def _materialize_project_secret_refs(
        self,
        *,
        session,
        tenant_id: str,
        project_id: str,
        raw_secret_refs: dict | None,
        encryption_key: str,
    ) -> dict[str, str]:
        normalized_map = self._normalize_string_map(raw_secret_refs)
        materialized: dict[str, str] = {}
        for key, raw_value in normalized_map.items():
            variable_name = str(key or "").strip()
            candidate = str(raw_value or "").strip()
            if not variable_name or not candidate:
                continue
            managed_ref = self._project_secret_ref(
                tenant_id=tenant_id,
                project_id=project_id,
                secret_key=variable_name,
            )

            normalized_candidate: str | None
            try:
                normalized_candidate = normalize_secret_ref(candidate)
            except ValueError:
                normalized_candidate = None

            if normalized_candidate is not None:
                if normalized_candidate == managed_ref:
                    materialized[variable_name] = managed_ref
                    continue
                if normalized_candidate.startswith(("platform/", "tenant/", "project/")):
                    materialized[variable_name] = normalized_candidate
                    continue
                if "/" not in normalized_candidate and ":" not in normalized_candidate:
                    materialized[variable_name] = normalized_candidate
                    continue

            tenant_secret_service.upsert_secret(
                session=session,
                secret_ref=managed_ref,
                plaintext_value=candidate,
                encryption_key=encryption_key,
                tenant_id=tenant_id,
            )
            materialized[variable_name] = managed_ref
        return materialized

    def _project_and_tenant_or_404(self, *, session, tenant_id: str, project_id: str):  # noqa: ANN001
        project = session.get(Project, project_id)
        if project is None or project.tenant_id != tenant_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
        tenant = session.get(Tenant, tenant_id)
        if tenant is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
        return project, tenant

    def _commit_project_update(self, *, session, project: Project, tenant: Tenant) -> object:  # noqa: ANN001
        self._sync_tenant_jira_project_keys(session, tenant=tenant)
        tenant.updated_at = project.updated_at
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A project with the same repository or Jira project key already exists for this tenant",
            ) from exc
        session.refresh(project)
        return self._project_to_schema(project, tenant_policy=tenant.policy_config)

    def list_projects(self, *, session, tenant_id: str) -> list[object]:
        tenant = session.get(Tenant, tenant_id)
        if tenant is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

        projects = session.execute(
            select(Project).where(Project.tenant_id == tenant_id).order_by(Project.created_at)
        ).scalars().all()
        return [self._project_to_schema(project, tenant_policy=tenant.policy_config) for project in projects]

    def list_project_navigation(self, *, session, tenant_id: str) -> list[ProjectNavigationRead]:
        tenant = session.get(Tenant, tenant_id)
        if tenant is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

        rows = session.execute(
            select(Project.project_id, Project.tenant_id, Project.name, Project.jira_project_key, Project.is_archived)
            .where(Project.tenant_id == tenant_id)
            .order_by(Project.name)
        ).all()
        return [
            ProjectNavigationRead(
                project_id=row.project_id,
                tenant_id=row.tenant_id,
                name=row.name,
                jira_project_key=row.jira_project_key,
                is_archived=row.is_archived,
            )
            for row in rows
        ]

    def get_project(self, *, session, tenant_id: str, project_id: str) -> object:
        project = session.get(Project, project_id)
        if project is None or project.tenant_id != tenant_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
        tenant = session.get(Tenant, tenant_id)
        if tenant is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
        return self._project_to_schema(project, tenant_policy=tenant.policy_config)

    def create_project(self, *, session, tenant_id: str, payload) -> object:  # noqa: ANN001
        tenant = session.get(Tenant, tenant_id)
        if tenant is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

        normalized_name = payload.name.strip()
        normalized_repo = self._normalize_project_repo(payload.github_repository)
        normalized_jira_key = self._normalize_project_key(payload.jira_project_key)
        if not normalized_name or not normalized_repo or not normalized_jira_key:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Project name, repository, and Jira key are required",
            )

        now = datetime.now(timezone.utc)
        normalized_policy_overrides = self._normalize_project_policy_overrides(payload.policy_overrides)

        try:
            normalized_architecture_docs_config = self._normalize_project_architecture_docs_config(
                self._optional_payload_dict(getattr(payload, "architecture_docs", None))
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

        settings = self._settings_factory()
        project = Project(
            project_id=allocate_project_id(session),
            tenant_id=tenant_id,
            name=normalized_name,
            github_repository=normalized_repo,
            jira_project_key=normalized_jira_key,
            policy_overrides=normalized_policy_overrides,
            architecture_docs_config=normalized_architecture_docs_config,
            environment=self._normalize_string_map(payload.environment),
            secret_refs={},
            discord_config={},
            deployment_config={},
            is_archived=False,
            created_at=now,
            updated_at=now,
        )
        try:
            project.secret_refs = self._materialize_project_secret_refs(
                session=session,
                tenant_id=tenant_id,
                project_id=project.project_id,
                raw_secret_refs=payload.secret_refs,
                encryption_key=str(getattr(settings, "secrets_encryption_key", "") or "").strip(),
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        normalized_discord = self._normalize_project_discord_config(
            self._optional_payload_dict(getattr(payload, "discord", None))
        )
        if self._should_bind_project_discord_channel(
            tenant=tenant,
            discord_config=normalized_discord,
            is_archived=False,
            force_bind=payload.discord is not None,
        ):
            try:
                normalized_discord = self._resolve_project_discord_channel_binding(
                    session=session,
                    settings=settings,
                    tenant=tenant,
                    project=project,
                    discord_config=normalized_discord,
                )
            except (DiscordApiError, ValueError) as exc:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"Unable to provision Discord channel: {exc}",
                ) from exc
        project.discord_config = normalized_discord
        session.add(project)
        tenant.updated_at = now
        self._sync_tenant_jira_project_keys(session, tenant=tenant)
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A project with the same repository or Jira project key already exists for this tenant",
            ) from exc
        try:
            self._ensure_project_repository_checkout(
                session=session,
                tenant=tenant,
                project=project,
            )
        except ProjectRepoCheckoutError as exc:
            session.rollback()
            persisted_project = session.get(Project, project.project_id)
            if persisted_project is not None:
                session.delete(persisted_project)
            self._sync_tenant_jira_project_keys(session, tenant=tenant)
            session.commit()
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Unable to clone project repository: {exc}",
            ) from exc
        session.refresh(project)
        return self._project_to_schema(project, tenant_policy=tenant.policy_config)

    def get_project_deployment_policy(self, *, session, tenant_id: str, project_id: str) -> object:
        tenant = session.get(Tenant, tenant_id)
        if tenant is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
        project = session.get(Project, project_id)
        if project is None or project.tenant_id != tenant_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
        return project_deployment_policy_to_schema(project)

    def update_project_deployment_policy(
        self,
        *,
        session,
        tenant_id: str,
        project_id: str,
        payload: ProjectDeploymentPolicyWrite,
    ) -> object:
        tenant = session.get(Tenant, tenant_id)
        if tenant is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
        project = session.get(Project, project_id)
        if project is None or project.tenant_id != tenant_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
        project.deployment_config = normalize_project_deployment_policy(payload)
        project.updated_at = datetime.now(timezone.utc)
        session.commit()
        session.refresh(project)
        return project_deployment_policy_to_schema(project)

    def complete_project_deployment_setup(
        self,
        *,
        session,
        tenant_id: str,
        project_id: str,
        payload: ProjectDeploymentPolicyWrite,
        requested_by_user_id: str | None,
    ) -> object:
        tenant = session.get(Tenant, tenant_id)
        if tenant is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
        project = session.get(Project, project_id)
        if project is None or project.tenant_id != tenant_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
        if not payload.enabled:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Deployment setup completion requires deployments to be enabled",
            )
        project.deployment_config = normalize_project_deployment_policy(payload)
        project.updated_at = datetime.now(timezone.utc)
        session.commit()
        session.refresh(project)
        policy = project_deployment_policy_to_schema(project)
        try:
            start_result = start_project_deployment_setup_workflow(
                session=session,
                settings=self._settings_factory(),
                tenant_id=tenant_id,
                project_id=project_id,
                project_name=str(project.name or project_id),
                execution_key=project_deployment_setup_execution_key(tenant_id=tenant_id, project_id=project_id),
                production_branch=str(policy.production_branch or ""),
                requested_by_user_id=requested_by_user_id,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Failed to start deployment setup workflow: {exc}",
            ) from exc
        return ProjectDeploymentSetupStartRead(
            workflow_id=start_result.workflow_id,
            status=start_result.status,
            policy=policy,
        )

    def get_project_app_deployment_config(self, *, session, tenant_id: str, project_id: str, app_id: str) -> object:
        tenant = session.get(Tenant, tenant_id)
        if tenant is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
        project = session.get(Project, project_id)
        if project is None or project.tenant_id != tenant_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
        app = get_project_app(session=session, tenant_id=tenant_id, project_id=project_id, app_id=app_id)
        if app is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project app not found")
        return project_deployment_config_to_schema(app)

    def update_project_app_deployment_config(
        self,
        *,
        session,
        tenant_id: str,
        project_id: str,
        app_id: str,
        payload,
    ) -> object:  # noqa: ANN001
        tenant = session.get(Tenant, tenant_id)
        if tenant is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
        project = session.get(Project, project_id)
        if project is None or project.tenant_id != tenant_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
        app = get_project_app(session=session, tenant_id=tenant_id, project_id=project_id, app_id=app_id)
        if app is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project app not found")
        normalized_config = normalize_project_deployment_config(payload)
        app.deployment_config = normalized_config
        app.updated_at = datetime.now(timezone.utc)
        project.updated_at = app.updated_at
        session.commit()
        session.refresh(app)
        return project_deployment_config_to_schema(app)

    def list_project_app_deployment_releases(
        self,
        *,
        session,
        tenant_id: str,
        project_id: str,
        app_id: str,
    ) -> list[object]:
        return list_project_deployment_releases(
            session=session,
            tenant_id=tenant_id,
            project_id=project_id,
            app_id=app_id,
        )

    def create_project_app_deployment_release(
        self,
        *,
        session,
        tenant_id: str,
        project_id: str,
        app_id: str,
        payload: ProjectDeploymentReleaseCreate,
        requested_by_user_id: str | None,
    ) -> object:
        return create_project_deployment_release(
            session=session,
            tenant_id=tenant_id,
            project_id=project_id,
            payload=payload.model_copy(update={"app_id": app_id}),
            requested_by_user_id=requested_by_user_id,
        )

    def get_project_app_deployment_release(
        self,
        *,
        session,
        tenant_id: str,
        project_id: str,
        app_id: str,
        release_id: str,
    ) -> object:
        return get_project_deployment_release(
            session=session,
            tenant_id=tenant_id,
            project_id=project_id,
            release_id=release_id,
            app_id=app_id,
        )

    def get_project_app_deployment_release_logs(
        self,
        *,
        session,
        tenant_id: str,
        project_id: str,
        app_id: str,
        release_id: str,
    ) -> object:
        from orchestrator.api.admin.deployment_release_service import get_project_deployment_release_logs

        return get_project_deployment_release_logs(
            session=session,
            tenant_id=tenant_id,
            project_id=project_id,
            release_id=release_id,
            app_id=app_id,
        )

    def update_project_app_deployment_release_status(
        self,
        *,
        session,
        tenant_id: str,
        project_id: str,
        app_id: str,
        release_id: str,
        payload,
    ) -> object:  # noqa: ANN001
        return update_project_deployment_release_status(
            session=session,
            tenant_id=tenant_id,
            project_id=project_id,
            release_id=release_id,
            payload=payload,
            app_id=app_id,
        )

    def list_project_apps(
        self,
        *,
        session,
        tenant_id: str,
        project_id: str,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[object]:
        tenant = session.get(Tenant, tenant_id)
        if tenant is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
        project = session.get(Project, project_id)
        if project is None or project.tenant_id != tenant_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
        if limit is not None and limit < 1:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="limit must be greater than 0")
        if offset < 0:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="offset must be greater than or equal to 0")
        query = (
            select(ProjectApp)
            .where(
                ProjectApp.tenant_id == tenant_id,
                ProjectApp.project_id == project_id,
                ProjectApp.source_path == ".",
            )
            .order_by(ProjectApp.created_at.asc(), ProjectApp.app_id.asc())
            .offset(offset)
        )
        if limit is not None:
            query = query.limit(limit)
        apps = session.execute(query).scalars().all()
        return [
            _project_app_to_schema(
                app,
                latest_release=_latest_deployment_release(
                    session=session,
                    tenant_id=tenant_id,
                    project_id=project_id,
                    app_id=app.app_id,
                    include_legacy_default_release=app.source_path == ".",
                ),
            )
            for app in apps
        ]

    def create_project_app(self, *, session, tenant_id: str, project_id: str, payload: ProjectAppCreate) -> object:  # noqa: ANN001
        tenant = session.get(Tenant, tenant_id)
        if tenant is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
        project = session.get(Project, project_id)
        if project is None or project.tenant_id != tenant_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

        now = datetime.now(timezone.utc)
        app = ProjectApp(
            app_id=str(uuid4()),
            tenant_id=tenant_id,
            project_id=project_id,
            name=payload.name.strip(),
            slug=payload.slug,
            source_path=payload.source_path,
            detection_confidence=payload.detection_confidence,
            detected_runtime=payload.detected_runtime,
            detected_language=payload.detected_language,
            analysis_source=_normalize_optional_string(payload.analysis_source),
            build_strategy=payload.build_strategy,
            exposed_port=payload.exposed_port,
            healthcheck=payload.healthcheck,
            start_command=payload.start_command,
            env_schema_json=dict(payload.env_schema_json or {}),
            secret_schema_json=dict(payload.secret_schema_json or {}),
            deployment_config=normalize_project_deployment_config(payload.deployment_config),
            status="draft",
            created_at=now,
            updated_at=now,
        )
        session.add(app)
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="An app with the same slug or source path already exists for this project",
            ) from exc
        session.refresh(app)
        return _project_app_to_schema(
            app,
            latest_release=_latest_deployment_release(
                session=session,
                tenant_id=tenant_id,
                project_id=project_id,
                app_id=app.app_id,
                include_legacy_default_release=app.source_path == ".",
            ),
        )

    def get_project_app(self, *, session, tenant_id: str, project_id: str, app_id: str) -> object:
        tenant = session.get(Tenant, tenant_id)
        if tenant is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
        project = session.get(Project, project_id)
        if project is None or project.tenant_id != tenant_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
        app = get_project_app(session=session, tenant_id=tenant_id, project_id=project_id, app_id=app_id)
        if app is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project app not found")
        _require_project_level_deployment(app)
        return _project_app_to_schema(app)

    def update_project_app(
        self,
        *,
        session,
        tenant_id: str,
        project_id: str,
        app_id: str,
        payload: ProjectAppUpdate,
    ) -> object:  # noqa: ANN001
        tenant = session.get(Tenant, tenant_id)
        if tenant is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
        project = session.get(Project, project_id)
        if project is None or project.tenant_id != tenant_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
        app = get_project_app(session=session, tenant_id=tenant_id, project_id=project_id, app_id=app_id)
        if app is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project app not found")

        updates = payload.model_dump(exclude_none=True)
        if app.source_path == "." and "source_path" in updates and updates["source_path"] != ".":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="The default app source_path must remain '.'",
            )
        deployment_config_payload = payload.deployment_config
        if deployment_config_payload is not None:
            updates.pop("deployment_config", None)
            app.deployment_config = normalize_project_deployment_config(deployment_config_payload)
        if "name" in updates:
            updates["name"] = str(updates["name"]).strip()
        for field_name, value in updates.items():
            setattr(app, field_name, value)
        app.updated_at = datetime.now(timezone.utc)
        project.updated_at = app.updated_at
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="An app with the same slug or source path already exists for this project",
            ) from exc
        session.refresh(app)
        return _project_app_to_schema(app)

    def delete_project_app(self, *, session, tenant_id: str, project_id: str, app_id: str) -> None:  # noqa: ANN001
        tenant = session.get(Tenant, tenant_id)
        if tenant is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
        project = session.get(Project, project_id)
        if project is None or project.tenant_id != tenant_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
        app = get_project_app(session=session, tenant_id=tenant_id, project_id=project_id, app_id=app_id)
        if app is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project app not found")

        active_release = session.execute(
            select(ProjectDeploymentRelease.release_id)
            .where(
                ProjectDeploymentRelease.tenant_id == tenant_id,
                ProjectDeploymentRelease.project_id == project_id,
                ProjectDeploymentRelease.app_id == app_id,
                ProjectDeploymentRelease.status.in_(("queued", "provisioning", "deploying")),
            )
            .limit(1)
        ).scalar_one_or_none()
        if active_release is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="App has an active deployment. Wait for it to finish before removing the app.",
            )

        active_command = session.execute(
            select(DeploymentHostCommand.command_id)
            .where(
                DeploymentHostCommand.tenant_id == tenant_id,
                DeploymentHostCommand.project_id == project_id,
                DeploymentHostCommand.app_id == app_id,
                DeploymentHostCommand.status.in_(("queued", "claimed", "running")),
            )
            .limit(1)
        ).scalar_one_or_none()
        if active_command is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="App has an active deployment host command. Wait for it to finish before removing the app.",
            )

        project.updated_at = datetime.now(timezone.utc)
        session.delete(app)
        session.commit()

    def list_project_app_analysis_runs(self, *, session, tenant_id: str, project_id: str) -> list[object]:
        tenant = session.get(Tenant, tenant_id)
        if tenant is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
        project = session.get(Project, project_id)
        if project is None or project.tenant_id != tenant_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
        runs = session.execute(
            select(ProjectAppAnalysisRun)
            .where(
                ProjectAppAnalysisRun.tenant_id == tenant_id,
                ProjectAppAnalysisRun.project_id == project_id,
            )
            .order_by(ProjectAppAnalysisRun.created_at.desc())
        ).scalars().all()
        return [_project_app_analysis_run_to_schema(run) for run in runs]

    def get_project_app_analysis_run(
        self,
        *,
        session,
        tenant_id: str,
        project_id: str,
        run_id: str,
    ) -> object:
        tenant = session.get(Tenant, tenant_id)
        if tenant is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
        project = session.get(Project, project_id)
        if project is None or project.tenant_id != tenant_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
        run = session.get(ProjectAppAnalysisRun, run_id)
        if run is None or run.tenant_id != tenant_id or run.project_id != project_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project app analysis run not found")
        return _project_app_analysis_run_to_schema(run)

    def start_project_app_analysis_run(
        self,
        *,
        session,
        tenant_id: str,
        project_id: str,
        payload: ProjectAppAnalysisRunStart,
        requested_by_user_id: str | None,
    ) -> object:  # noqa: ANN001
        tenant = session.get(Tenant, tenant_id)
        if tenant is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
        project = session.get(Project, project_id)
        if project is None or project.tenant_id != tenant_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
        try:
            self._ensure_project_repository_checkout(
                session=session,
                tenant=tenant,
                project=project,
            )
        except ProjectRepoCheckoutError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Unable to ensure project repository checkout: {exc}",
            ) from exc
        now = datetime.now(timezone.utc)
        settings = self._settings_factory()
        checkout_path = str(
            project_repo_dir(
                base_dir=str(getattr(settings, "project_repo_checkout_base_dir", "") or ""),
                tenant_id=tenant_id,
                project_id=project_id,
            )
        )
        request_payload = payload.model_dump(exclude_none=True)
        if requested_by_user_id is not None:
            request_payload["requested_by_user_id"] = requested_by_user_id
        request_payload["analysis_source"] = "manual_analyze"
        request_payload["checkout_path"] = checkout_path
        run = ProjectAppAnalysisRun(
            run_id=str(uuid4()),
            tenant_id=tenant_id,
            project_id=project_id,
            status="queued",
            planner_version=_normalize_optional_string(payload.planner_version),
            request_payload=request_payload,
            result_payload={},
            error=None,
            created_at=now,
            started_at=None,
            completed_at=None,
            updated_at=now,
        )
        session.add(run)
        enqueue_result = enqueue_webhook_job(
            session,
            request=WebhookJobEnqueueRequest(
                transport=WEBHOOK_TRANSPORT_PROJECT_APP_ANALYSIS,
                request_id=run.run_id,
                tenant_id=tenant_id,
                project_id=project_id,
                subject_key=f"project_app_analysis:{tenant_id}:{project_id}:{run.run_id}",
                dedupe_key=run.run_id,
                event_type="project_app_analysis",
                payload_json={
                    "analysis_run_id": run.run_id,
                    "checkout_path": checkout_path,
                    "analysis_source": "manual_analyze",
                    "planner_version": _normalize_optional_string(payload.planner_version),
                },
                context_json={
                    "analysis_run_id": run.run_id,
                    "checkout_path": checkout_path,
                    "analysis_source": "manual_analyze",
                },
            ),
            now=now,
        )
        notify_webhook_job_enqueued(
            session=session,
            transport=enqueue_result.job.transport,
            tenant_id=enqueue_result.job.tenant_id,
            project_id=enqueue_result.job.project_id,
            subject_key=enqueue_result.job.subject_key,
            job_id=enqueue_result.job.job_id,
            dedupe_key=enqueue_result.job.dedupe_key,
        )
        session.commit()
        session.refresh(run)
        return _project_app_analysis_run_to_schema(run)

    def get_tenant_deployments_overview(self, *, session, tenant_id: str) -> object:
        tenant = session.get(Tenant, tenant_id)
        if tenant is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
        projects = session.execute(
            select(Project).where(Project.tenant_id == tenant_id).order_by(Project.name.asc())
        ).scalars().all()
        project_names = {project.project_id: project.name for project in projects}
        apps = session.execute(
            select(ProjectApp)
            .where(ProjectApp.tenant_id == tenant_id)
            .order_by(ProjectApp.updated_at.desc(), ProjectApp.created_at.desc())
        ).scalars().all()

        app_rows: list[TenantDeploymentsOverviewAppRead] = []
        failure_rows: list[TenantDeploymentsOverviewFailureRead] = []
        summary_counts = {
            "draft_count": 0,
            "needs_pr_merge_count": 0,
            "ready_count": 0,
            "deploying_count": 0,
            "live_count": 0,
            "failed_count": 0,
        }
        status_to_count_key = {
            "draft": "draft_count",
            "needs_pr_merge": "needs_pr_merge_count",
            "ready": "ready_count",
            "deploying": "deploying_count",
            "live": "live_count",
            "failed": "failed_count",
        }

        for app in apps:
            latest_release = _latest_deployment_release(
                session=session,
                tenant_id=tenant_id,
                project_id=app.project_id,
                app_id=app.app_id,
                include_legacy_default_release=app.source_path == ".",
            )
            last_release_status = _normalize_optional_string(getattr(latest_release, "status", None))
            last_error = _normalize_optional_string(getattr(latest_release, "last_error", None))
            status_value = str(app.status or "").strip().lower()
            status_count_key = status_to_count_key.get(status_value)
            if status_count_key is not None:
                summary_counts[status_count_key] = summary_counts.get(status_count_key, 0) + 1
            project_name = project_names.get(app.project_id, app.project_id)
            app_rows.append(
                TenantDeploymentsOverviewAppRead(
                    tenant_id=tenant_id,
                    tenant_name=tenant.name,
                    project_id=app.project_id,
                    project_name=project_name,
                    app_id=app.app_id,
                    app_name=app.name,
                    slug=app.slug,
                    source_path=_normalize_optional_string(app.source_path),
                    status=app.status,
                    detection_confidence=app.detection_confidence,
                    detected_runtime=_normalize_optional_string(app.detected_runtime),
                    detected_language=_normalize_optional_string(app.detected_language),
                    build_strategy=_normalize_optional_string(app.build_strategy),
                    last_release_status=last_release_status,
                    last_error=last_error,
                    updated_at=app.updated_at,
                )
            )
            if status_value == "failed":
                failure_rows.append(
                    TenantDeploymentsOverviewFailureRead(
                        tenant_id=tenant_id,
                        tenant_name=tenant.name,
                        project_id=app.project_id,
                        project_name=project_name,
                        app_id=app.app_id,
                        app_name=app.name,
                        slug=app.slug,
                        source_path=_normalize_optional_string(app.source_path),
                        status=app.status,
                        last_error=last_error,
                        last_release_id=getattr(latest_release, "release_id", None),
                        updated_at=app.updated_at,
                    )
                )

        failure_rows = sorted(failure_rows, key=lambda row: row.updated_at, reverse=True)[:10]
        return TenantDeploymentsOverviewRead(
            summary=TenantDeploymentsOverviewSummaryRead(
                total_apps=len(app_rows),
                draft_count=summary_counts["draft_count"],
                needs_pr_merge_count=summary_counts["needs_pr_merge_count"],
                ready_count=summary_counts["ready_count"],
                deploying_count=summary_counts["deploying_count"],
                live_count=summary_counts["live_count"],
                failed_count=summary_counts["failed_count"],
            ),
            latest_failures=failure_rows,
            apps=app_rows,
            generated_at=datetime.now(timezone.utc),
        )

    def apply_project_app_deployment_resources(
        self,
        *,
        session,
        tenant_id: str,
        project_id: str,
        app_id: str,
        payload: ProjectDeploymentResourceApplyRequest,
    ) -> ProjectDeploymentOperationRead:
        return apply_project_deployment_resources(
            session=session,
            tenant_id=tenant_id,
            project_id=project_id,
            payload=payload,
            app_id=app_id,
        )

    def apply_project_app_deployment_volumes(
        self,
        *,
        session,
        tenant_id: str,
        project_id: str,
        app_id: str,
        payload: ProjectDeploymentVolumeApplyRequest,
    ) -> ProjectDeploymentOperationRead:
        return apply_project_deployment_volumes(
            session=session,
            tenant_id=tenant_id,
            project_id=project_id,
            payload=payload,
            app_id=app_id,
        )

    def apply_project_app_deployment_domains(
        self,
        *,
        session,
        tenant_id: str,
        project_id: str,
        app_id: str,
        payload: ProjectDeploymentDomainApplyRequest,
    ) -> ProjectDeploymentOperationRead:
        return apply_project_deployment_domains(
            session=session,
            tenant_id=tenant_id,
            project_id=project_id,
            payload=payload,
            app_id=app_id,
        )

    def apply_project_app_deployment_backups(
        self,
        *,
        session,
        tenant_id: str,
        project_id: str,
        app_id: str,
        payload: ProjectDeploymentBackupApplyRequest,
    ) -> ProjectDeploymentOperationRead:
        return apply_project_deployment_backups(
            session=session,
            tenant_id=tenant_id,
            project_id=project_id,
            payload=payload,
            app_id=app_id,
        )

    def trigger_project_app_deployment_backups(
        self,
        *,
        session,
        tenant_id: str,
        project_id: str,
        app_id: str,
        payload: ProjectDeploymentBackupTriggerRequest,
    ) -> ProjectDeploymentOperationRead:
        return trigger_project_deployment_backups(
            session=session,
            tenant_id=tenant_id,
            project_id=project_id,
            payload=payload,
            app_id=app_id,
        )

    def restore_project_app_deployment_backup(
        self,
        *,
        session,
        tenant_id: str,
        project_id: str,
        app_id: str,
        payload: ProjectDeploymentBackupRestoreRequest,
        requested_by_user_id: str | None = None,
    ) -> ProjectDeploymentRestoreRunRead:
        return create_project_deployment_restore_run(
            session=session,
            tenant_id=tenant_id,
            project_id=project_id,
            app_id=app_id,
            payload=payload,
            requested_by_user_id=requested_by_user_id,
        )

    def list_project_app_deployment_backup_executions(
        self,
        *,
        session,
        tenant_id: str,
        project_id: str,
        app_id: str,
        backup_key: str,
    ) -> ProjectDeploymentBackupExecutionListRead:
        return list_project_deployment_backup_executions(
            session=session,
            tenant_id=tenant_id,
            project_id=project_id,
            app_id=app_id,
            backup_key=backup_key,
        )

    def list_project_app_deployment_restore_runs(
        self,
        *,
        session,
        tenant_id: str,
        project_id: str,
        app_id: str,
    ) -> list[ProjectDeploymentRestoreRunRead]:
        return list_project_deployment_restore_runs(
            session=session,
            tenant_id=tenant_id,
            project_id=project_id,
            app_id=app_id,
        )

    def get_project_app_deployment_restore_run(
        self,
        *,
        session,
        tenant_id: str,
        project_id: str,
        app_id: str,
        restore_run_id: str,
    ) -> ProjectDeploymentRestoreRunRead:
        return get_project_deployment_restore_run(
            session=session,
            tenant_id=tenant_id,
            project_id=project_id,
            app_id=app_id,
            restore_run_id=restore_run_id,
        )


    def update_project_configuration(self, *, session, tenant_id: str, project_id: str, payload) -> object:  # noqa: ANN001
        project, tenant = self._project_and_tenant_or_404(session=session, tenant_id=tenant_id, project_id=project_id)
        normalized_name = payload.name.strip()
        normalized_repo = self._normalize_project_repo(payload.github_repository)
        normalized_jira_key = self._normalize_project_key(payload.jira_project_key)
        if not normalized_name or not normalized_repo or not normalized_jira_key:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Project name, repository, and Jira key are required",
            )
        try:
            normalized_architecture_docs_config = self._normalize_project_architecture_docs_config(
                self._optional_payload_dict(getattr(payload, "architecture_docs", None))
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

        jira_key_changed = str(project.jira_project_key or "").strip().upper() != normalized_jira_key
        project.name = normalized_name
        project.github_repository = normalized_repo
        project.jira_project_key = normalized_jira_key
        project.architecture_docs_config = normalized_architecture_docs_config
        if jira_key_changed:
            policy_overrides = dict(project.policy_overrides or {})
            policy_overrides.pop("run_board_id", None)
            project.policy_overrides = policy_overrides
        project.updated_at = datetime.now(timezone.utc)
        return self._commit_project_update(session=session, project=project, tenant=tenant)

    def update_project_policy(self, *, session, tenant_id: str, project_id: str, payload) -> object:  # noqa: ANN001
        project, tenant = self._project_and_tenant_or_404(session=session, tenant_id=tenant_id, project_id=project_id)
        project.policy_overrides = self._normalize_project_policy_overrides(payload.policy_overrides)
        project.updated_at = datetime.now(timezone.utc)
        return self._commit_project_update(session=session, project=project, tenant=tenant)

    def update_project_environment(self, *, session, tenant_id: str, project_id: str, payload) -> object:  # noqa: ANN001
        project, tenant = self._project_and_tenant_or_404(session=session, tenant_id=tenant_id, project_id=project_id)
        project.environment = self._normalize_string_map(payload.environment)
        project.updated_at = datetime.now(timezone.utc)
        return self._commit_project_update(session=session, project=project, tenant=tenant)

    def update_project_secret_refs(self, *, session, tenant_id: str, project_id: str, payload) -> object:  # noqa: ANN001
        project, tenant = self._project_and_tenant_or_404(session=session, tenant_id=tenant_id, project_id=project_id)
        settings = self._settings_factory()
        try:
            project.secret_refs = self._materialize_project_secret_refs(
                session=session,
                tenant_id=tenant_id,
                project_id=project.project_id,
                raw_secret_refs=payload.secret_refs,
                encryption_key=str(getattr(settings, "secrets_encryption_key", "") or "").strip(),
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        project.updated_at = datetime.now(timezone.utc)
        return self._commit_project_update(session=session, project=project, tenant=tenant)

    def update_project_discord(self, *, session, tenant_id: str, project_id: str, payload) -> object:  # noqa: ANN001
        project, tenant = self._project_and_tenant_or_404(session=session, tenant_id=tenant_id, project_id=project_id)
        settings = self._settings_factory()
        normalized_discord = self._with_preserved_discord_system_fields(
            existing=dict(project.discord_config or {}),
            proposed=self._normalize_project_discord_config(
                self._optional_payload_dict(getattr(payload, "discord", None))
            ),
        )
        if self._should_bind_project_discord_channel(
            tenant=tenant,
            discord_config=normalized_discord,
            is_archived=bool(project.is_archived),
            force_bind=getattr(payload, "discord", None) is not None,
        ):
            try:
                normalized_discord = self._resolve_project_discord_channel_binding(
                    session=session,
                    settings=settings,
                    tenant=tenant,
                    project=project,
                    discord_config=normalized_discord,
                )
            except (DiscordApiError, ValueError) as exc:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"Unable to provision Discord channel: {exc}",
                ) from exc
        project.discord_config = normalized_discord
        project.updated_at = datetime.now(timezone.utc)
        return self._commit_project_update(session=session, project=project, tenant=tenant)

    def update_project_archive_state(self, *, session, tenant_id: str, project_id: str, payload) -> object:  # noqa: ANN001
        project, tenant = self._project_and_tenant_or_404(session=session, tenant_id=tenant_id, project_id=project_id)
        project.is_archived = bool(payload.is_archived)
        project.updated_at = datetime.now(timezone.utc)
        return self._commit_project_update(session=session, project=project, tenant=tenant)

    def resolve_project_jira_run_board(self, *, session, tenant_id: str, project_id: str) -> object:  # noqa: ANN001
        project, tenant = self._project_and_tenant_or_404(session=session, tenant_id=tenant_id, project_id=project_id)
        normalized_jira_key = self._normalize_project_key(project.jira_project_key)
        try:
            run_board_id = self._resolve_project_run_board_id(
                session=session,
                tenant=tenant,
                jira_project_key=normalized_jira_key,
                settings=self._settings_factory(),
            )
        except (ValueError, AtlassianOAuthError) as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Unable to resolve Jira board for project {normalized_jira_key}: {exc}",
            ) from exc
        if run_board_id is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Unable to find Jira board for project {normalized_jira_key}",
            )
        policy_overrides = dict(project.policy_overrides or {})
        policy_overrides["run_board_id"] = run_board_id
        project.policy_overrides = policy_overrides
        project.updated_at = datetime.now(timezone.utc)
        return self._commit_project_update(session=session, project=project, tenant=tenant)

def _coerce_dict(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return dict(value)
    return {}


def _normalize_optional_string(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _project_app_to_schema(app: ProjectApp, *, latest_release: ProjectDeploymentRelease | None = None) -> ProjectAppRead:
    return ProjectAppRead(
        app_id=app.app_id,
        tenant_id=app.tenant_id,
        project_id=app.project_id,
        name=app.name,
        slug=app.slug,
        source_path=app.source_path,
        detection_confidence=app.detection_confidence,
        detected_runtime=app.detected_runtime,
        detected_language=app.detected_language,
        analysis_source=app.analysis_source,
        build_strategy=app.build_strategy,
        exposed_port=app.exposed_port,
        healthcheck=app.healthcheck,
        start_command=app.start_command,
        env_schema_json=_coerce_dict(app.env_schema_json),
        secret_schema_json=_coerce_dict(app.secret_schema_json),
        deployment_config=project_deployment_config_to_schema(app),
        status=app.status,
        latest_release_id=latest_release.release_id if latest_release is not None else None,
        latest_release_status=latest_release.status if latest_release is not None else None,
        latest_release_name=(
            f"{latest_release.git_ref} @ {latest_release.commit_sha[:8]}" if latest_release is not None else None
        ),
        latest_release_git_ref=latest_release.git_ref if latest_release is not None else None,
        latest_release_commit_sha=latest_release.commit_sha if latest_release is not None else None,
        last_error=_normalize_optional_string(latest_release.last_error) if latest_release is not None else None,
        created_at=app.created_at,
        updated_at=app.updated_at,
    )


def _project_app_analysis_run_to_schema(run: ProjectAppAnalysisRun) -> ProjectAppAnalysisRunRead:
    return ProjectAppAnalysisRunRead(
        run_id=run.run_id,
        tenant_id=run.tenant_id,
        project_id=run.project_id,
        status=run.status,
        planner_version=run.planner_version,
        request_payload=_coerce_dict(run.request_payload),
        result_payload=_coerce_dict(run.result_payload),
        error=run.error,
        created_at=run.created_at,
        started_at=run.started_at,
        completed_at=run.completed_at,
        updated_at=run.updated_at,
    )


def _compact_payload(payload: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in payload.items() if value is not None}


def _item_result(
    *,
    key: str,
    kind: str | None = None,
    status: str,
    provider_uuid: str | None = None,
    message: str | None = None,
    details: dict[str, object] | None = None,
) -> ProjectDeploymentOperationItemRead:
    return ProjectDeploymentOperationItemRead(
        key=key,
        kind=kind,
        status=status,  # type: ignore[arg-type]
        provider_uuid=provider_uuid,
        message=message,
        details=details or {},
    )


def _operation_result(
    *,
    operation: str,
    tenant_id: str,
    project_id: str,
    provider: str,
    items: list[ProjectDeploymentOperationItemRead],
    application_uuid: str | None = None,
) -> ProjectDeploymentOperationRead:
    counts = {
        "applied": 0,
        "skipped": 0,
        "unsupported": 0,
        "failed": 0,
    }
    for item in items:
        counts[item.status] = counts.get(item.status, 0) + 1
    return ProjectDeploymentOperationRead(
        operation=operation,  # type: ignore[arg-type]
        tenant_id=tenant_id,
        project_id=project_id,
        provider=provider,
        application_uuid=application_uuid,
        items=items,
        applied_count=counts["applied"],
        skipped_count=counts["skipped"],
        unsupported_count=counts["unsupported"],
        failed_count=counts["failed"],
        executed_at=datetime.now(timezone.utc),
    )


def _latest_deployment_release(
    *,
    session,
    tenant_id: str,
    project_id: str,
    app_id: str | None = None,
    include_legacy_default_release: bool = False,
) -> ProjectDeploymentRelease | None:
    query = select(ProjectDeploymentRelease).where(
        ProjectDeploymentRelease.tenant_id == tenant_id,
        ProjectDeploymentRelease.project_id == project_id,
        ProjectDeploymentRelease.release_kind == "production",
    )
    normalized_app_id = _normalize_optional_string(app_id)
    if normalized_app_id is None:
        query = query.where(ProjectDeploymentRelease.app_id.is_(None))
    elif include_legacy_default_release:
        query = query.where(
            (ProjectDeploymentRelease.app_id == normalized_app_id) | ProjectDeploymentRelease.app_id.is_(None)
        )
    else:
        query = query.where(ProjectDeploymentRelease.app_id == normalized_app_id)
    return session.execute(query.order_by(ProjectDeploymentRelease.created_at.desc())).scalars().first()


def _latest_application_uuid(release: ProjectDeploymentRelease | None) -> str | None:
    if release is None:
        return None
    provider_context = _coerce_dict(release.provider_context)
    return _normalize_optional_string(provider_context.get("application_uuid"))


def _is_project_level_deployment(app: ProjectApp) -> bool:
    return str(app.source_path or "").strip() == "."


def _require_project_level_deployment(app: ProjectApp) -> None:
    if _is_project_level_deployment(app):
        return
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Deployment not found",
    )


def _build_internal_coolify_client(
    *,
    session,
    tenant,
    tenant_plane: TenantDeploymentPlaneRead,
    project: Project,
) -> tuple[CoolifyApiClient | None, str | None]:  # noqa: ANN001
    if tenant_plane.provider != _INTERNAL_COOLIFY_PROVIDER:
        return None, "Tenant deployment plane is not configured for internal Coolify"
    if tenant_plane.state not in _ACTIVE_DEPLOYMENT_PLANE_STATES:
        return None, "Tenant deployment plane is not active"

    api_token_ref = _normalize_optional_string((tenant_plane.secret_refs or {}).get("coolify_api_token"))
    if api_token_ref is None:
        return None, "Tenant deployment plane is missing coolify_api_token secret ref"
    settings = get_settings()
    api_token = _resolve_secret_value(
        session=session,
        secret_ref=api_token_ref,
        tenant_id=tenant.tenant_id,
        project_id=project.project_id,
        encryption_key=settings.secrets_encryption_key,
    )
    if api_token is None:
        return None, "Tenant deployment plane references missing Coolify API token"
    try:
        api_base_url = _coolify_api_base_url(tenant_plane=tenant_plane)
    except HTTPException as exc:
        return None, str(exc.detail)
    return CoolifyApiClient(CoolifyApiConfig(base_url=api_base_url, bearer_token=api_token)), None


def _deployment_context(
    *,
    session,
    tenant_id: str,
    project_id: str,
    app_id: str | None = None,
) -> tuple[Tenant, Project, ProjectApp, TenantDeploymentPlaneRead, ProjectDeploymentConfigRead, ProjectDeploymentRelease | None]:
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    project = session.get(Project, project_id)
    if project is None or project.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    selected_app = (
        get_project_app(session=session, tenant_id=tenant_id, project_id=project_id, app_id=app_id)
        if _normalize_optional_string(app_id) is not None
        else get_project_default_app(session=session, tenant_id=tenant_id, project_id=project_id)
    )
    if selected_app is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project app not found")
    _require_project_level_deployment(selected_app)
    return (
        tenant,
        project,
        selected_app,
        tenant_deployment_plane_to_schema(tenant),
        project_deployment_config_to_schema(selected_app),
        _latest_deployment_release(
            session=session,
            tenant_id=tenant_id,
            project_id=project_id,
            app_id=selected_app.app_id,
            include_legacy_default_release=selected_app.source_path == ".",
        ),
    )

def _selected_items_by_key(items: list[object], selected_keys: list[str]) -> list[object]:
    if not selected_keys:
        return list(items)
    selected = {str(key).strip().lower() for key in selected_keys if str(key or "").strip()}
    return [item for item in items if str(getattr(item, "key", "")).strip().lower() in selected]


def _merge_item_config(item: object, updates: dict[str, object]) -> None:
    current_config = _coerce_dict(getattr(item, "config", {}))
    current_config.update(updates)
    setattr(item, "config", current_config)


def _resource_database_type(resource: ProjectDeploymentResourceWrite) -> str | None:
    kind = str(resource.kind or "").strip().lower()
    if kind in _DATABASE_RESOURCE_KINDS:
        return kind
    if kind == "cache":
        config = _coerce_dict(resource.config)
        return _normalize_optional_string(config.get("database_type") or config.get("coolify_database_type")) or "redis"
    if kind == "database":
        config = _coerce_dict(resource.config)
        return _normalize_optional_string(config.get("database_type") or config.get("coolify_database_type")) or "postgres"
    return None


def _build_database_payload(
    *,
    session,
    tenant_id: str,
    project_id: str,
    tenant_plane: TenantDeploymentPlaneRead,
    project_deployment: ProjectDeploymentConfigRead,
    resource: ProjectDeploymentResourceWrite,
    database_type: str,
) -> dict[str, object]:
    config = _coerce_dict(resource.config)
    payload: dict[str, object] = {
        "server_uuid": _normalize_optional_string(tenant_plane.coolify_server_uuid),
        "project_uuid": _normalize_optional_string(tenant_plane.coolify_project_uuid),
        "environment_name": _normalize_optional_string(tenant_plane.coolify_environment_name or project_deployment.environment_name),
        "destination_uuid": _normalize_optional_string(tenant_plane.coolify_destination_uuid),
        "instant_deploy": True,
        "name": _normalize_optional_string(resource.name) or resource.key,
        "description": _normalize_optional_string(config.get("description"))
        or f"Managed by Master Builder for tenant {tenant_id}, project {project_id}",
    }
    common_keys = {
        "image",
        "is_public",
        "public_port",
        "limits_memory",
        "limits_memory_swap",
        "limits_memory_swappiness",
        "limits_memory_reservation",
        "limits_cpus",
        "limits_cpuset",
        "limits_cpu_shares",
        "postgres_user",
        "postgres_password",
        "postgres_db",
        "postgres_initdb_args",
        "postgres_host_auth_method",
        "postgres_conf",
        "clickhouse_admin_user",
        "clickhouse_admin_password",
        "dragonfly_password",
        "redis_password",
        "redis_conf",
        "keydb_password",
        "keydb_conf",
        "mariadb_root_password",
        "mariadb_password",
        "mariadb_user",
        "mariadb_database",
        "mariadb_conf",
        "mongo_initdb_root_username",
        "mongo_initdb_root_password",
        "mongo_initdb_database",
        "mongo_conf",
        "mysql_root_password",
        "mysql_password",
        "mysql_user",
        "mysql_database",
        "mysql_conf",
    }
    settings = get_settings()
    encryption_key = str(getattr(settings, "secrets_encryption_key", "") or "").strip()
    for key in common_keys:
        value = config.get(key)
        secret_ref = _normalize_optional_string(config.get(f"{key}_secret_ref"))
        if value is None and secret_ref is not None:
            value = _resolve_secret_value(
                session=session,
                secret_ref=secret_ref,
                tenant_id=tenant_id,
                project_id=project_id,
                encryption_key=encryption_key,
            )
            if value is None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Resource '{resource.key}' references missing secret for {key}",
                )
        if value is not None:
            payload[key] = value
    return _compact_payload(payload)


def _build_storage_payload(volume: ProjectDeploymentVolumeWrite) -> dict[str, object] | None:
    config = _coerce_dict(volume.config)
    mount_path = _normalize_optional_string(config.get("mount_path"))
    if mount_path is None:
        return None
    payload: dict[str, object] = {
        "type": volume.type,
        "mount_path": mount_path,
        "name": _normalize_optional_string(volume.name) or volume.key,
    }
    for key in ("host_path", "content", "fs_path", "is_directory"):
        value = config.get(key)
        if value is not None:
            payload[key] = value
    return _compact_payload(payload)


def _build_service_payload(
    *,
    tenant_plane: TenantDeploymentPlaneRead,
    project_deployment: ProjectDeploymentConfigRead,
    resource: ProjectDeploymentResourceWrite,
) -> dict[str, object] | None:
    config = _coerce_dict(resource.config)
    service_type = _normalize_optional_string(config.get("service_type") or config.get("type"))
    if service_type is None and str(resource.kind or "").strip().lower() in {"object_storage", "s3", "minio_s3"}:
        service_type = "minio"
    if service_type is None:
        return None
    payload: dict[str, object] = {
        "type": service_type,
        "name": _normalize_optional_string(resource.name) or resource.key,
        "server_uuid": _normalize_optional_string(tenant_plane.coolify_server_uuid),
        "project_uuid": _normalize_optional_string(tenant_plane.coolify_project_uuid),
        "environment_name": _normalize_optional_string(tenant_plane.coolify_environment_name or project_deployment.environment_name),
        "instant_deploy": True,
    }
    destination_uuid = _normalize_optional_string(tenant_plane.coolify_destination_uuid)
    if destination_uuid is not None:
        payload["destination"] = destination_uuid
    docker_compose = _normalize_optional_string(config.get("docker_compose"))
    if docker_compose is not None:
        payload["docker_compose"] = docker_compose
    environment_uuid = _normalize_optional_string(config.get("environment_uuid"))
    if environment_uuid is not None:
        payload["environment"] = environment_uuid
    return _compact_payload(payload)


def _build_database_backup_payload(
    *,
    tenant_plane: TenantDeploymentPlaneRead,
    project_deployment: ProjectDeploymentConfigRead,
    backup_policy: ProjectDeploymentBackupPolicyWrite,
) -> dict[str, object]:
    config = _coerce_dict(backup_policy.config)
    payload: dict[str, object] = {
        "frequency": _normalize_optional_string(backup_policy.schedule),
        "enabled": backup_policy.enabled,
    }
    for key in (
        "save_s3",
        "s3_storage_uuid",
        "databases_to_backup",
        "dump_all",
        "database_backup_retention_amount_locally",
        "database_backup_retention_days_locally",
        "database_backup_retention_max_storage_locally",
        "database_backup_retention_amount_s3",
        "database_backup_retention_days_s3",
        "database_backup_retention_max_storage_s3",
        "timeout",
        "disable_local_backup",
    ):
        value = config.get(key)
        if value is not None:
            payload[key] = value
    if payload.get("s3_storage_uuid") is None:
        payload.pop("s3_storage_uuid", None)
    if payload.get("frequency") is None:
        payload.pop("frequency", None)
    return payload


def _deployment_operation_from_items(
    *,
    operation: str,
    tenant_id: str,
    project_id: str,
    provider: str,
    items: list[ProjectDeploymentOperationItemRead],
    application_uuid: str | None = None,
) -> ProjectDeploymentOperationRead:
    return _operation_result(
        operation=operation,
        tenant_id=tenant_id,
        project_id=project_id,
        provider=provider,
        items=items,
        application_uuid=application_uuid,
    )


def _store_deployment_config(
    *,
    session,
    tenant_id: str,
    project: Project,
    project_app: ProjectApp,
    deployment_config: ProjectDeploymentConfigRead,
) -> None:  # noqa: ANN001
    _ = tenant_id
    _ = project
    serialized = deployment_config.model_dump(exclude_none=True)
    serialized.pop("enabled", None)
    project_app.deployment_config = serialized


def apply_project_deployment_resources(
    *,
    session,
    tenant_id: str,
    project_id: str,
    payload: ProjectDeploymentResourceApplyRequest,
    app_id: str | None = None,
) -> ProjectDeploymentOperationRead:  # noqa: ANN001
    tenant, project, project_app, tenant_plane, project_deployment, latest_release = _deployment_context(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        app_id=app_id,
    )
    selected_resources = _selected_items_by_key(list(project_deployment.resources), payload.resource_keys)
    client, client_error = _build_internal_coolify_client(
        session=session,
        tenant=tenant,
        tenant_plane=tenant_plane,
        project=project,
    )
    application_uuid = _latest_application_uuid(latest_release)
    updated = False
    items: list[ProjectDeploymentOperationItemRead] = []
    for resource in selected_resources:
        resource_kind = str(resource.kind or "").strip().lower()
        if resource_kind in _UNSUPPORTED_RESOURCE_KINDS:
            items.append(
                _item_result(
                    key=resource.key,
                    kind=resource.kind,
                    status="unsupported",
                    message=f"Resource kind '{resource.kind}' is not supported by the Coolify API surface",
                )
            )
            continue

        database_type = _resource_database_type(resource)
        if database_type is not None:
            if client is None:
                items.append(
                    _item_result(
                        key=resource.key,
                        kind=resource.kind,
                        status="failed",
                        message=client_error,
                    )
                )
                continue
            try:
                payload_data = _build_database_payload(
                    session=session,
                    tenant_id=tenant_id,
                    project_id=project_id,
                    tenant_plane=tenant_plane,
                    project_deployment=project_deployment,
                    resource=resource,
                    database_type=database_type,
                )
                existing_uuid = _normalize_optional_string(_coerce_dict(resource.config).get("coolify_uuid"))
                if existing_uuid is None:
                    provider_uuid = client.create_database(database_type=database_type, payload=payload_data)
                else:
                    client.update_database(database_uuid=existing_uuid, payload=payload_data)
                    provider_uuid = existing_uuid
                _merge_item_config(
                    resource,
                    {
                        "coolify_uuid": provider_uuid,
                        "coolify_database_type": database_type,
                    },
                )
                updated = True
                items.append(
                    _item_result(
                        key=resource.key,
                        kind=resource.kind,
                        status="applied",
                        provider_uuid=provider_uuid,
                        details={"database_type": database_type},
                    )
                )
            except CoolifyApiError as exc:
                items.append(
                    _item_result(
                        key=resource.key,
                        kind=resource.kind,
                        status="failed",
                        message=str(exc),
                    )
                )
            continue

        if resource_kind in _SERVICE_RESOURCE_KINDS:
            if client is None:
                items.append(
                    _item_result(
                        key=resource.key,
                        kind=resource.kind,
                        status="failed",
                        message=client_error,
                    )
                )
                continue
            payload_data = _build_service_payload(
                tenant_plane=tenant_plane,
                project_deployment=project_deployment,
                resource=resource,
            )
            if payload_data is None:
                items.append(
                    _item_result(
                        key=resource.key,
                        kind=resource.kind,
                        status="failed",
                        message="Service resources require service_type",
                    )
                )
                continue
            try:
                existing_uuid = _normalize_optional_string(_coerce_dict(resource.config).get("coolify_uuid"))
                if existing_uuid is None:
                    provider_uuid = client.create_service(payload=payload_data)
                else:
                    client.update_service(service_uuid=existing_uuid, payload=payload_data)
                    provider_uuid = existing_uuid
                _merge_item_config(
                    resource,
                    {
                        "coolify_uuid": provider_uuid,
                        "coolify_service_type": _normalize_optional_string(payload_data.get("type")),
                    },
                )
                updated = True
                items.append(
                    _item_result(
                        key=resource.key,
                        kind=resource.kind,
                        status="applied",
                        provider_uuid=provider_uuid,
                        details={"service_type": payload_data.get("type")},
                    )
                )
            except CoolifyApiError as exc:
                items.append(
                    _item_result(
                        key=resource.key,
                        kind=resource.kind,
                        status="failed",
                        message=str(exc),
                    )
                )
            continue

        items.append(
            _item_result(
                key=resource.key,
                kind=resource.kind,
                status="unsupported",
                message=f"Resource kind '{resource.kind}' is not supported",
            )
        )

    if updated:
        _store_deployment_config(
            session=session,
            tenant_id=tenant_id,
            project=project,
            project_app=project_app,
            deployment_config=project_deployment,
        )
        project.updated_at = datetime.now(timezone.utc)
        session.commit()
        session.refresh(project)

    return _deployment_operation_from_items(
        operation="apply_resources",
        tenant_id=tenant_id,
        project_id=project_id,
        provider=_INTERNAL_COOLIFY_PROVIDER,
        items=items,
        application_uuid=_latest_application_uuid(latest_release) or application_uuid,
    )


def apply_project_deployment_volumes(
    *,
    session,
    tenant_id: str,
    project_id: str,
    payload: ProjectDeploymentVolumeApplyRequest,
    app_id: str | None = None,
) -> ProjectDeploymentOperationRead:  # noqa: ANN001
    tenant, project, project_app, tenant_plane, project_deployment, latest_release = _deployment_context(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        app_id=app_id,
    )
    selected_volumes = _selected_items_by_key(list(project_deployment.volumes), payload.volume_keys)
    client, client_error = _build_internal_coolify_client(
        session=session,
        tenant=tenant,
        tenant_plane=tenant_plane,
        project=project,
    )
    application_uuid = _latest_application_uuid(latest_release)
    updated = False
    items: list[ProjectDeploymentOperationItemRead] = []
    for volume in selected_volumes:
        if client is None:
            items.append(
                _item_result(
                    key=volume.key,
                    kind=volume.type,
                    status="failed",
                    message=client_error,
                )
            )
            continue
        if application_uuid is None:
            items.append(
                _item_result(
                    key=volume.key,
                    kind=volume.type,
                    status="skipped",
                    message="No Coolify application UUID is available yet for deployment volumes",
                )
            )
            continue
        payload_data = _build_storage_payload(volume)
        if payload_data is None:
            items.append(
                _item_result(
                    key=volume.key,
                    kind=volume.type,
                    status="failed",
                    message="Deployment volumes require mount_path",
                )
            )
            continue
        try:
            existing_uuid = _normalize_optional_string(_coerce_dict(volume.config).get("coolify_uuid"))
            if existing_uuid is None:
                provider_uuid = client.create_application_storage(
                    application_uuid=application_uuid,
                    payload=payload_data,
                )
            else:
                payload_with_uuid = dict(payload_data)
                payload_with_uuid["uuid"] = existing_uuid
                client.update_application_storage(
                    application_uuid=application_uuid,
                    payload=payload_with_uuid,
                )
                provider_uuid = existing_uuid
            if provider_uuid is not None:
                _merge_item_config(
                    volume,
                    {
                        "coolify_uuid": provider_uuid,
                        "coolify_application_uuid": application_uuid,
                    },
                )
                updated = True
            items.append(
                _item_result(
                    key=volume.key,
                    kind=volume.type,
                    status="applied",
                    provider_uuid=provider_uuid,
                    details={"application_uuid": application_uuid},
                )
            )
        except CoolifyApiError as exc:
            items.append(
                _item_result(
                    key=volume.key,
                    kind=volume.type,
                    status="failed",
                    message=str(exc),
                )
            )

    if updated:
        _store_deployment_config(
            session=session,
            tenant_id=tenant_id,
            project=project,
            project_app=project_app,
            deployment_config=project_deployment,
        )
        project.updated_at = datetime.now(timezone.utc)
        session.commit()
        session.refresh(project)

    return _deployment_operation_from_items(
        operation="apply_volumes",
        tenant_id=tenant_id,
        project_id=project_id,
        provider=_INTERNAL_COOLIFY_PROVIDER,
        items=items,
        application_uuid=_latest_application_uuid(latest_release) or application_uuid,
    )


def apply_project_deployment_domains(
    *,
    session,
    tenant_id: str,
    project_id: str,
    payload: ProjectDeploymentDomainApplyRequest,
    app_id: str | None = None,
) -> ProjectDeploymentOperationRead:  # noqa: ANN001
    tenant, project, project_app, tenant_plane, project_deployment, latest_release = _deployment_context(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        app_id=app_id,
    )
    selected_domains = _selected_items_by_key(list(project_deployment.domains), payload.domain_keys)
    client, client_error = _build_internal_coolify_client(
        session=session,
        tenant=tenant,
        tenant_plane=tenant_plane,
        project=project,
    )
    application_uuid = _latest_application_uuid(latest_release)
    if client is None:
        items = [
            _item_result(
                key=domain.key,
                kind="domain",
                status="failed",
                message=client_error,
            )
            for domain in selected_domains
        ]
        return _deployment_operation_from_items(
            operation="apply_domains",
            tenant_id=tenant_id,
            project_id=project_id,
            provider=_INTERNAL_COOLIFY_PROVIDER,
            items=items,
            application_uuid=application_uuid,
        )

    if application_uuid is None:
        items = [
            _item_result(
                key=domain.key,
                kind="domain",
                status="skipped",
                message="No Coolify application UUID is available yet for domain updates",
            )
            for domain in selected_domains
        ]
        return _deployment_operation_from_items(
            operation="apply_domains",
            tenant_id=tenant_id,
            project_id=project_id,
            provider=_INTERNAL_COOLIFY_PROVIDER,
            items=items,
        )

    if not selected_domains:
        return _deployment_operation_from_items(
            operation="apply_domains",
            tenant_id=tenant_id,
            project_id=project_id,
            provider=_INTERNAL_COOLIFY_PROVIDER,
            items=[],
            application_uuid=application_uuid,
        )

    domains_value = ",".join(
        f"{domain.host}{domain.path or ''}" for domain in selected_domains if _normalize_optional_string(domain.host)
    )
    force_https_enabled = any(bool(domain.tls_enabled) for domain in selected_domains)
    try:
        client.update_application(
            application_uuid=application_uuid,
            payload={
                "domains": domains_value,
                "is_force_https_enabled": force_https_enabled,
            },
        )
        updated = False
        for domain in selected_domains:
            _merge_item_config(
                domain,
                {
                    "coolify_application_uuid": application_uuid,
                    "coolify_domains": domains_value,
                    "coolify_force_https": force_https_enabled,
                },
            )
            updated = True
        if updated:
            _store_deployment_config(
                session=session,
                tenant_id=tenant_id,
                project=project,
                project_app=project_app,
                deployment_config=project_deployment,
            )
            project.updated_at = datetime.now(timezone.utc)
            session.commit()
            session.refresh(project)
        items = [
            _item_result(
                key=domain.key,
                kind="domain",
                status="applied",
                provider_uuid=application_uuid,
                details={
                    "domains": domains_value,
                    "force_https_enabled": force_https_enabled,
                },
            )
            for domain in selected_domains
        ]
    except CoolifyApiError as exc:
        items = [
            _item_result(
                key=domain.key,
                kind="domain",
                status="failed",
                message=str(exc),
            )
            for domain in selected_domains
        ]
    return _deployment_operation_from_items(
        operation="apply_domains",
        tenant_id=tenant_id,
        project_id=project_id,
        provider=_INTERNAL_COOLIFY_PROVIDER,
        items=items,
        application_uuid=application_uuid,
    )


def apply_project_deployment_backups(
    *,
    session,
    tenant_id: str,
    project_id: str,
    payload: ProjectDeploymentBackupApplyRequest,
    app_id: str | None = None,
) -> ProjectDeploymentOperationRead:  # noqa: ANN001
    tenant, project, project_app, tenant_plane, project_deployment, latest_release = _deployment_context(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        app_id=app_id,
    )
    selected_backups = _selected_items_by_key(list(project_deployment.backup_policies), payload.backup_keys)
    client, client_error = _build_internal_coolify_client(
        session=session,
        tenant=tenant,
        tenant_plane=tenant_plane,
        project=project,
    )
    updated = False
    items: list[ProjectDeploymentOperationItemRead] = []
    resource_by_key = {resource.key: resource for resource in project_deployment.resources}
    for backup_policy in selected_backups:
        resource = resource_by_key.get(backup_policy.resource_key)
        if resource is None:
            items.append(
                _item_result(
                    key=backup_policy.key,
                    kind="backup",
                    status="failed",
                    message=f"Backup policy '{backup_policy.key}' references unknown resource '{backup_policy.resource_key}'",
                )
            )
            continue
        database_type = _resource_database_type(resource)
        if database_type is None:
            items.append(
                _item_result(
                    key=backup_policy.key,
                    kind="backup",
                    status="unsupported",
                    message=f"Backup policy '{backup_policy.key}' targets unsupported resource kind '{resource.kind}'",
                )
            )
            continue
        if client is None:
            items.append(
                _item_result(
                    key=backup_policy.key,
                    kind="backup",
                    status="failed",
                    message=client_error,
                )
            )
            continue
        database_uuid = _normalize_optional_string(_coerce_dict(resource.config).get("coolify_uuid"))
        if database_uuid is None:
            items.append(
                _item_result(
                    key=backup_policy.key,
                    kind="backup",
                    status="skipped",
                    message=f"Resource '{resource.key}' has not been applied to Coolify yet",
                )
            )
            continue
        try:
            payload_data = _build_database_backup_payload(
                tenant_plane=tenant_plane,
                project_deployment=project_deployment,
                backup_policy=backup_policy,
            )
            existing_backup_uuid = _normalize_optional_string(_coerce_dict(backup_policy.config).get("coolify_backup_uuid"))
            if existing_backup_uuid is None:
                backup_uuid = client.create_database_backup(database_uuid=database_uuid, payload=payload_data)
            else:
                client.update_database_backup(
                    database_uuid=database_uuid,
                    backup_uuid=existing_backup_uuid,
                    payload=payload_data,
                )
                backup_uuid = existing_backup_uuid
            _merge_item_config(
                backup_policy,
                {
                    "coolify_database_uuid": database_uuid,
                    "coolify_backup_uuid": backup_uuid,
                    "coolify_database_type": database_type,
                },
            )
            updated = True
            items.append(
                _item_result(
                    key=backup_policy.key,
                    kind="backup",
                    status="applied",
                    provider_uuid=backup_uuid,
                    details={"database_uuid": database_uuid, "database_type": database_type},
                )
            )
        except CoolifyApiError as exc:
            items.append(
                _item_result(
                    key=backup_policy.key,
                    kind="backup",
                    status="failed",
                    message=str(exc),
                )
            )
    if updated:
        _store_deployment_config(
            session=session,
            tenant_id=tenant_id,
            project=project,
            project_app=project_app,
            deployment_config=project_deployment,
        )
        project.updated_at = datetime.now(timezone.utc)
        session.commit()
        session.refresh(project)
    return _deployment_operation_from_items(
        operation="apply_backups",
        tenant_id=tenant_id,
        project_id=project_id,
        provider=_INTERNAL_COOLIFY_PROVIDER,
        items=items,
        application_uuid=_latest_application_uuid(latest_release),
    )


def trigger_project_deployment_backups(
    *,
    session,
    tenant_id: str,
    project_id: str,
    payload: ProjectDeploymentBackupTriggerRequest,
    app_id: str | None = None,
) -> ProjectDeploymentOperationRead:  # noqa: ANN001
    tenant, project, project_app, tenant_plane, project_deployment, latest_release = _deployment_context(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        app_id=app_id,
    )
    selected_backups = _selected_items_by_key(list(project_deployment.backup_policies), payload.backup_keys)
    client, client_error = _build_internal_coolify_client(
        session=session,
        tenant=tenant,
        tenant_plane=tenant_plane,
        project=project,
    )
    items: list[ProjectDeploymentOperationItemRead] = []
    resource_by_key = {resource.key: resource for resource in project_deployment.resources}
    for backup_policy in selected_backups:
        resource = resource_by_key.get(backup_policy.resource_key)
        if resource is None:
            items.append(
                _item_result(
                    key=backup_policy.key,
                    kind="backup",
                    status="failed",
                    message=f"Backup policy '{backup_policy.key}' references unknown resource '{backup_policy.resource_key}'",
                )
            )
            continue
        database_type = _resource_database_type(resource)
        if database_type is None:
            items.append(
                _item_result(
                    key=backup_policy.key,
                    kind="backup",
                    status="unsupported",
                    message=f"Backup policy '{backup_policy.key}' targets unsupported resource kind '{resource.kind}'",
                )
            )
            continue
        if client is None:
            items.append(
                _item_result(
                    key=backup_policy.key,
                    kind="backup",
                    status="failed",
                    message=client_error,
                )
            )
            continue
        database_uuid = _normalize_optional_string(_coerce_dict(resource.config).get("coolify_uuid"))
        backup_uuid = _normalize_optional_string(_coerce_dict(backup_policy.config).get("coolify_backup_uuid"))
        if database_uuid is None or backup_uuid is None:
            items.append(
                _item_result(
                    key=backup_policy.key,
                    kind="backup",
                    status="skipped",
                    message=f"Backup policy '{backup_policy.key}' is not yet applied to Coolify",
                )
            )
            continue
        try:
            client.trigger_database_backup(database_uuid=database_uuid, backup_uuid=backup_uuid)
            _merge_item_config(
                backup_policy,
                {
                    "coolify_last_triggered_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            items.append(
                _item_result(
                    key=backup_policy.key,
                    kind="backup",
                    status="applied",
                    provider_uuid=backup_uuid,
                    details={"database_uuid": database_uuid, "database_type": database_type},
                )
            )
        except CoolifyApiError as exc:
            items.append(
                _item_result(
                    key=backup_policy.key,
                    kind="backup",
                    status="failed",
                    message=str(exc),
                )
            )

    _store_deployment_config(
        session=session,
        tenant_id=tenant_id,
        project=project,
        project_app=project_app,
        deployment_config=project_deployment,
    )
    project.updated_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(project)
    return _deployment_operation_from_items(
        operation="trigger_backups",
        tenant_id=tenant_id,
        project_id=project_id,
        provider=_INTERNAL_COOLIFY_PROVIDER,
        items=items,
        application_uuid=_latest_application_uuid(latest_release),
    )
