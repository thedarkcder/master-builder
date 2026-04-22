from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from orchestrator.storage.models import Project, Tenant
from orchestrator.core.secret_manager import normalize_secret_ref
from orchestrator.core.tenant_secret_service import tenant_secret_service
from orchestrator.tools.discord_api import DiscordApiError
from orchestrator.tools.atlassian_oauth import AtlassianOAuthError
from orchestrator.tools.project_repo_checkout import ProjectRepoCheckoutError


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
        except (ValueError, AtlassianOAuthError) as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Unable to resolve Jira board for project {normalized_jira_key}: {exc}",
            ) from exc
        if run_board_id is not None:
            normalized_policy_overrides["run_board_id"] = run_board_id

        try:
            normalized_architecture_docs_config = self._normalize_project_architecture_docs_config(
                self._optional_payload_dict(getattr(payload, "architecture_docs", None))
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

        settings = self._settings_factory()
        project = Project(
            project_id=str(uuid4()),
            tenant_id=tenant_id,
            name=normalized_name,
            github_repository=normalized_repo,
            jira_project_key=normalized_jira_key,
            policy_overrides=normalized_policy_overrides,
            architecture_docs_config=normalized_architecture_docs_config,
            environment=self._normalize_string_map(payload.environment),
            secret_refs={},
            discord_config={},
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
        except (ValueError, AtlassianOAuthError) as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Unable to resolve Jira board for project {normalized_jira_key}: {exc}",
            ) from exc
        if run_board_id is not None:
            normalized_policy_overrides["run_board_id"] = run_board_id
        try:
            normalized_architecture_docs_config = self._normalize_project_architecture_docs_config(
                self._optional_payload_dict(getattr(payload, "architecture_docs", None))
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

        project.policy_overrides = normalized_policy_overrides
        project.architecture_docs_config = normalized_architecture_docs_config
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
                self._optional_payload_dict(getattr(payload, "discord", None))
            ),
        )
        if self._should_bind_project_discord_channel(
            tenant=tenant,
            discord_config=normalized_discord,
            is_archived=bool(payload.is_archived),
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
