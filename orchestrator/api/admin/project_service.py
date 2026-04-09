from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from orchestrator.api.admin.deployment_config_service import (
    ensure_project_default_app,
    get_project_app,
    normalize_project_deployment_config,
    project_deployment_config_to_schema,
    tenant_deployment_plane_to_schema,
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
    ProjectDeploymentBackupApplyRequest,
    ProjectDeploymentBackupRestoreRequest,
    ProjectDeploymentBackupTriggerRequest,
    ProjectDeploymentConfigRead,
    ProjectDeploymentDomainApplyRequest,
    ProjectDeploymentOperationItemRead,
    ProjectDeploymentOperationRead,
    ProjectDeploymentReleaseCreate,
    ProjectDeploymentBackupPolicyWrite,
    ProjectDeploymentResourceApplyRequest,
    ProjectDeploymentResourceWrite,
    ProjectAppAnalysisRunRead,
    ProjectAppAnalysisRunStart,
    ProjectAppCreate,
    ProjectAppRead,
    ProjectAppUpdate,
    TenantDeploymentsOverviewAppRead,
    TenantDeploymentsOverviewFailureRead,
    TenantDeploymentsOverviewRead,
    TenantDeploymentsOverviewSummaryRead,
    TenantDeploymentPlaneRead,
)
from orchestrator.core.config import get_settings
from orchestrator.core.webhook_job_queue import (
    WEBHOOK_TRANSPORT_PROJECT_APP_ANALYSIS,
    WebhookJobEnqueueRequest,
    enqueue_webhook_job,
)
from orchestrator.core.secret_manager import normalize_secret_ref
from orchestrator.core.tenant_secret_service import tenant_secret_service
from orchestrator.storage.models import Project, ProjectApp, ProjectAppAnalysisRun, ProjectDeploymentRelease, Tenant
from orchestrator.storage.run_queue_events import notify_webhook_job_enqueued
from orchestrator.tools.coolify_api import CoolifyApiClient, CoolifyApiConfig, CoolifyApiError
from orchestrator.tools.discord_api import DiscordApiError
from orchestrator.tools.jira_oauth import JiraOAuthError
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
_SERVICE_RESOURCE_KINDS = {"service", "one_click_service", "object_storage", "s3", "minio_s3"}
_STORAGE_RESOURCE_KINDS = {"persistent", "persistent_volume", "volume", "file"}
_UNSUPPORTED_RESOURCE_KINDS: set[str] = set()


class AdminProjectService:
    def __init__(
        self,
        *,
        normalize_project_repo: Callable[[str], str],
        normalize_project_key: Callable[[str], str],
        normalize_project_policy_overrides: Callable[[dict | None], dict],
        normalize_string_map: Callable[[dict | None], dict],
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
        self._normalize_project_discord_config = normalize_project_discord_config
        self._with_preserved_discord_system_fields = with_preserved_discord_system_fields
        self._resolve_project_discord_channel_binding = resolve_project_discord_channel_binding
        self._sync_tenant_jira_project_keys = sync_tenant_jira_project_keys
        self._ensure_project_repository_checkout = ensure_project_repository_checkout
        self._resolve_project_run_board_id = resolve_project_run_board_id
        self._project_to_schema = project_to_schema
        self._settings_factory = settings_factory

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

            tenant_secret_service.upsert_secret(
                session=session,
                secret_ref=managed_ref,
                plaintext_value=candidate,
                encryption_key=encryption_key,
                tenant_id=tenant_id,
            )
            materialized[variable_name] = managed_ref
        return materialized

    def list_projects(self, *, session, tenant_id: str) -> list[object]:
        tenant = session.get(Tenant, tenant_id)
        if tenant is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

        projects = session.execute(
            select(Project).where(Project.tenant_id == tenant_id).order_by(Project.created_at)
        ).scalars().all()
        return [self._project_to_schema(project, tenant_policy=tenant.policy_config) for project in projects]

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
            run_board_id = self._resolve_project_run_board_id(
                session=session,
                tenant=tenant,
                jira_project_key=normalized_jira_key,
                settings=self._settings_factory(),
            )
        except (ValueError, JiraOAuthError) as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Unable to resolve Jira board for project {normalized_jira_key}: {exc}",
            ) from exc
        if run_board_id is not None:
            normalized_policy_overrides["run_board_id"] = run_board_id

        settings = self._settings_factory()
        project = Project(
            project_id=str(uuid4()),
            tenant_id=tenant_id,
            name=normalized_name,
            github_repository=normalized_repo,
            jira_project_key=normalized_jira_key,
            policy_overrides=normalized_policy_overrides,
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
            payload.discord.model_dump(exclude_unset=True) if payload.discord else None
        )
        if payload.discord is not None:
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
        ensure_project_default_app(session=session, tenant_id=tenant_id, project=project)
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
        if app.source_path == ".":
            project.deployment_config = normalized_config
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
        payload,
        requested_by_user_id: str | None,
    ) -> object:  # noqa: ANN001
        return create_project_deployment_release(
            session=session,
            tenant_id=tenant_id,
            project_id=project_id,
            payload=ProjectDeploymentReleaseCreate(
                app_id=app_id,
                git_ref=payload.git_ref,
                commit_sha=payload.commit_sha,
                reason=payload.reason,
            ),
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

    def list_project_apps(self, *, session, tenant_id: str, project_id: str) -> list[object]:
        tenant = session.get(Tenant, tenant_id)
        if tenant is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
        project = session.get(Project, project_id)
        if project is None or project.tenant_id != tenant_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
        ensure_project_default_app(session=session, tenant_id=tenant_id, project=project)
        apps = session.execute(
            select(ProjectApp)
            .where(ProjectApp.tenant_id == tenant_id, ProjectApp.project_id == project_id)
            .order_by(ProjectApp.created_at.asc())
        ).scalars().all()
        return [_project_app_to_schema(app) for app in apps]

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
        return _project_app_to_schema(app)

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
        if app.source_path == ".":
            project.deployment_config = dict(app.deployment_config)
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
        ensure_project_default_app(session=session, tenant_id=tenant_id, project=project)
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
    ) -> ProjectDeploymentOperationRead:
        return restore_project_deployment_backup(
            session=session,
            tenant_id=tenant_id,
            project_id=project_id,
            payload=payload,
            app_id=app_id,
        )

    def update_project(self, *, session, tenant_id: str, project_id: str, payload) -> object:  # noqa: ANN001
        project = session.get(Project, project_id)
        if project is None or project.tenant_id != tenant_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
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

        project.name = normalized_name
        project.github_repository = normalized_repo
        project.jira_project_key = normalized_jira_key
        normalized_policy_overrides = self._normalize_project_policy_overrides(payload.policy_overrides)
        settings = self._settings_factory()
        try:
            run_board_id = self._resolve_project_run_board_id(
                session=session,
                tenant=tenant,
                jira_project_key=normalized_jira_key,
                settings=settings,
            )
        except (ValueError, JiraOAuthError) as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Unable to resolve Jira board for project {normalized_jira_key}: {exc}",
            ) from exc
        if run_board_id is not None:
            normalized_policy_overrides["run_board_id"] = run_board_id
        project.policy_overrides = normalized_policy_overrides
        project.environment = self._normalize_string_map(payload.environment)
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
        normalized_discord = self._with_preserved_discord_system_fields(
            existing=dict(project.discord_config or {}),
            proposed=self._normalize_project_discord_config(
                payload.discord.model_dump(exclude_unset=True) if payload.discord else None
            ),
        )
        if payload.discord is not None:
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
        project.is_archived = payload.is_archived
        project.updated_at = datetime.now(timezone.utc)

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


def _coerce_dict(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return dict(value)
    return {}


def _normalize_optional_string(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _project_app_to_schema(app: ProjectApp) -> ProjectAppRead:
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
        else ensure_project_default_app(session=session, tenant_id=tenant_id, project=project)
    )
    if selected_app is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project app not found")
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
    for key in common_keys:
        value = config.get(key)
        if value is not None:
            payload[key] = value
    return _compact_payload(payload)


def _build_storage_payload(resource: ProjectDeploymentResourceWrite) -> dict[str, object] | None:
    config = _coerce_dict(resource.config)
    mount_path = _normalize_optional_string(config.get("mount_path"))
    if mount_path is None:
        return None
    payload: dict[str, object] = {
        "type": "file" if str(resource.kind or "").strip().lower() == "file" else "persistent",
        "mount_path": mount_path,
        "name": _normalize_optional_string(resource.name) or resource.key,
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
    project_app.deployment_config = deployment_config.model_dump(exclude_none=True)
    if project_app.source_path == ".":
        project.deployment_config = deployment_config.model_dump(exclude_none=True)


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

        if resource_kind in _STORAGE_RESOURCE_KINDS:
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
            if application_uuid is None:
                items.append(
                    _item_result(
                        key=resource.key,
                        kind=resource.kind,
                        status="skipped",
                        message="No Coolify application UUID is available yet for storage resources",
                    )
                )
                continue
            payload_data = _build_storage_payload(resource)
            if payload_data is None:
                items.append(
                    _item_result(
                        key=resource.key,
                        kind=resource.kind,
                        status="failed",
                        message="Storage resources require mount_path",
                    )
                )
                continue
            try:
                existing_uuid = _normalize_optional_string(_coerce_dict(resource.config).get("coolify_uuid"))
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
                        resource,
                        {
                            "coolify_uuid": provider_uuid,
                            "coolify_application_uuid": application_uuid,
                        },
                    )
                    updated = True
                items.append(
                    _item_result(
                        key=resource.key,
                        kind=resource.kind,
                        status="applied",
                        provider_uuid=provider_uuid,
                        details={"application_uuid": application_uuid},
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


def restore_project_deployment_backup(
    *,
    session,
    tenant_id: str,
    project_id: str,
    payload: ProjectDeploymentBackupRestoreRequest,
    app_id: str | None = None,
) -> ProjectDeploymentOperationRead:  # noqa: ANN001
    _tenant, _project, _project_app, _tenant_plane, project_deployment, latest_release = _deployment_context(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        app_id=app_id,
    )
    selected = next(
        (
            backup_policy
            for backup_policy in project_deployment.backup_policies
            if backup_policy.key == payload.backup_key
        ),
        None,
    )
    if selected is None:
        items = [
            _item_result(
                key=payload.backup_key,
                kind="backup",
                status="failed",
                message=f"Backup policy '{payload.backup_key}' was not found",
            )
        ]
        return _deployment_operation_from_items(
            operation="restore_backups",
            tenant_id=tenant_id,
            project_id=project_id,
            provider=_INTERNAL_COOLIFY_PROVIDER,
            items=items,
            application_uuid=_latest_application_uuid(latest_release),
        )

    items = [
        _item_result(
            key=selected.key,
            kind="backup",
            status="unsupported",
            message="Coolify restore API is not documented in the public API surface yet",
            details={
                "backup_uuid": _normalize_optional_string(payload.backup_uuid),
                "execution_uuid": _normalize_optional_string(payload.execution_uuid),
            },
        )
    ]
    return _deployment_operation_from_items(
        operation="restore_backups",
        tenant_id=tenant_id,
        project_id=project_id,
        provider=_INTERNAL_COOLIFY_PROVIDER,
        items=items,
        application_uuid=_latest_application_uuid(latest_release),
    )
