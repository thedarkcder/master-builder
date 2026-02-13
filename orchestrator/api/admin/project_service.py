from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from orchestrator.storage.models import Project, Tenant
from orchestrator.tools.discord_api import DiscordApiError
from orchestrator.tools.jira_oauth import JiraOAuthError
from orchestrator.tools.project_repo_checkout import ProjectRepoCheckoutError


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

        project = Project(
            project_id=str(uuid4()),
            tenant_id=tenant_id,
            name=normalized_name,
            github_repository=normalized_repo,
            jira_project_key=normalized_jira_key,
            policy_overrides=normalized_policy_overrides,
            environment=self._normalize_string_map(payload.environment),
            secret_refs=self._normalize_string_map(payload.secret_refs),
            discord_config={},
            is_archived=False,
            created_at=now,
            updated_at=now,
        )
        normalized_discord = self._normalize_project_discord_config(payload.discord.model_dump() if payload.discord else None)
        if payload.discord is not None:
            settings = self._settings_factory()
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
        project.policy_overrides = normalized_policy_overrides
        project.environment = self._normalize_string_map(payload.environment)
        project.secret_refs = self._normalize_string_map(payload.secret_refs)
        normalized_discord = self._with_preserved_discord_system_fields(
            existing=dict(project.discord_config or {}),
            proposed=self._normalize_project_discord_config(payload.discord.model_dump() if payload.discord else None),
        )
        if payload.discord is not None:
            settings = self._settings_factory()
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
