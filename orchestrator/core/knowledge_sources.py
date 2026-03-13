from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.api.admin.route_helpers import jira_oauth_client, refresh_jira_connection_tokens
from orchestrator.core.knowledge_base import KnowledgeSyncResult, sync_project_knowledge_from_jira
from orchestrator.storage.models import JiraOAuthConnection, KnowledgeSource, Project, Tenant

SUPPORTED_KNOWLEDGE_CONNECTORS = frozenset({"jira", "google_drive", "discord"})
SYNCABLE_KNOWLEDGE_CONNECTORS = frozenset({"jira"})
SCHEDULED_KNOWLEDGE_CONNECTORS = frozenset({"jira"})
SOURCE_STATUSES = frozenset({"active", "disabled"})
SOURCE_SYNC_MODES = frozenset({"manual", "scheduled"})


class KnowledgeSourceValidationError(ValueError):
    pass


class KnowledgeSourceSyncUnsupportedError(RuntimeError):
    pass


@dataclass(frozen=True)
class KnowledgeSourceConfig:
    connector_type: str
    display_name: str
    status: str
    sync_mode: str
    config_json: dict


def list_project_knowledge_sources(
    *,
    session: Session,
    tenant_id: str,
    project_id: str,
) -> list[KnowledgeSource]:
    return list(
        session.execute(
            select(KnowledgeSource)
            .where(
                KnowledgeSource.tenant_id == tenant_id,
                KnowledgeSource.project_id == project_id,
            )
            .order_by(KnowledgeSource.connector_type.asc(), KnowledgeSource.display_name.asc(), KnowledgeSource.created_at.asc())
        ).scalars()
    )


def get_project_knowledge_source(
    *,
    session: Session,
    tenant_id: str,
    project_id: str,
    source_id: str,
) -> KnowledgeSource | None:
    source = session.get(KnowledgeSource, source_id)
    if source is None or source.tenant_id != tenant_id or source.project_id != project_id:
        return None
    return source


def create_project_knowledge_source(
    *,
    session: Session,
    tenant: Tenant,
    project: Project,
    connector_type: str,
    display_name: str | None,
    status: str | None,
    sync_mode: str | None,
    config_json: dict | None,
) -> KnowledgeSource:
    normalized = normalize_knowledge_source_config(
        session=session,
        tenant=tenant,
        project=project,
        connector_type=connector_type,
        display_name=display_name,
        status=status,
        sync_mode=sync_mode,
        config_json=config_json,
        existing_source=None,
    )
    now = datetime.now(timezone.utc)
    source = KnowledgeSource(
        source_id=uuid4().hex,
        tenant_id=tenant.tenant_id,
        project_id=project.project_id,
        connector_type=normalized.connector_type,
        display_name=normalized.display_name,
        status=normalized.status,
        sync_mode=normalized.sync_mode,
        config_json=normalized.config_json,
        last_synced_at=None,
        last_error=None,
        created_at=now,
        updated_at=now,
    )
    session.add(source)
    session.commit()
    session.refresh(source)
    return source


def update_project_knowledge_source(
    *,
    session: Session,
    tenant: Tenant,
    project: Project,
    source: KnowledgeSource,
    display_name: str | None,
    status: str | None,
    sync_mode: str | None,
    config_json: dict | None,
) -> KnowledgeSource:
    normalized = normalize_knowledge_source_config(
        session=session,
        tenant=tenant,
        project=project,
        connector_type=source.connector_type,
        display_name=display_name if display_name is not None else source.display_name,
        status=status if status is not None else source.status,
        sync_mode=sync_mode if sync_mode is not None else source.sync_mode,
        config_json=config_json if config_json is not None else dict(source.config_json or {}),
        existing_source=source,
    )
    source.display_name = normalized.display_name
    source.status = normalized.status
    source.sync_mode = normalized.sync_mode
    source.config_json = normalized.config_json
    source.updated_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(source)
    return source


def delete_project_knowledge_source(*, session: Session, source: KnowledgeSource) -> None:
    session.delete(source)
    session.commit()


def sync_project_knowledge_source(
    *,
    session: Session,
    tenant: Tenant,
    project: Project,
    source: KnowledgeSource,
    settings,
) -> KnowledgeSyncResult:
    connector_type = str(source.connector_type or "").strip().lower()
    if connector_type != "jira":
        raise KnowledgeSourceSyncUnsupportedError(
            f"Connector '{connector_type}' does not provide a live sync adapter in this environment."
        )
    connection_id = str((tenant.jira_config or {}).get("connection_id") or "").strip()
    if not connection_id:
        raise KnowledgeSourceValidationError("Tenant Jira connection is not configured")
    connection = session.get(JiraOAuthConnection, connection_id)
    if connection is None:
        raise KnowledgeSourceValidationError("Jira OAuth connection was not found")
    project_key = str((source.config_json or {}).get("project_key") or project.jira_project_key or "").strip().upper()
    if not project_key:
        raise KnowledgeSourceValidationError("Jira source requires a project key")

    access_token = refresh_jira_connection_tokens(
        session,
        connection=connection,
        settings=settings,
        tenant_id=tenant.tenant_id,
    )
    client = jira_oauth_client(
        session=session,
        settings=settings,
        tenant_id=tenant.tenant_id,
        project_id=project.project_id,
    )
    try:
        result = sync_project_knowledge_from_jira(
            session=session,
            tenant_id=tenant.tenant_id,
            project_id=project.project_id,
            project_key=project_key,
            jira_client=client,
            access_token=access_token,
            cloud_id=connection.cloud_id,
            max_issues=max(1, int(getattr(settings, "knowledge_jira_sync_max_issues", 500))),
        )
    except Exception as exc:
        source.last_error = str(exc)
        source.updated_at = datetime.now(timezone.utc)
        session.commit()
        raise

    source.last_synced_at = datetime.now(timezone.utc)
    source.last_error = None
    source.updated_at = source.last_synced_at
    session.commit()
    session.refresh(source)
    return result


def normalize_knowledge_source_config(
    *,
    session: Session,
    tenant: Tenant,
    project: Project,
    connector_type: str,
    display_name: str | None,
    status: str | None,
    sync_mode: str | None,
    config_json: dict | None,
    existing_source: KnowledgeSource | None,
) -> KnowledgeSourceConfig:
    normalized_type = _normalize_connector_type(connector_type)
    normalized_status = _normalize_status(status)
    normalized_sync_mode = _normalize_sync_mode(sync_mode, connector_type=normalized_type)
    normalized_config = _normalize_config(connector_type=normalized_type, project=project, config_json=config_json or {})
    normalized_display_name = (display_name or _default_display_name(normalized_type, normalized_config)).strip()
    if not normalized_display_name:
        raise KnowledgeSourceValidationError("Knowledge source display_name is required")

    if normalized_type == "jira":
        existing_jira = session.execute(
            select(KnowledgeSource).where(
                KnowledgeSource.tenant_id == tenant.tenant_id,
                KnowledgeSource.project_id == project.project_id,
                KnowledgeSource.connector_type == "jira",
            )
        ).scalar_one_or_none()
        if existing_jira is not None and (existing_source is None or existing_jira.source_id != existing_source.source_id):
            raise KnowledgeSourceValidationError("Only one Jira knowledge source may be configured per project")

    return KnowledgeSourceConfig(
        connector_type=normalized_type,
        display_name=normalized_display_name,
        status=normalized_status,
        sync_mode=normalized_sync_mode,
        config_json=normalized_config,
    )


def build_knowledge_source_summary(*, source: KnowledgeSource) -> str:
    config_json = dict(source.config_json or {})
    connector_type = str(source.connector_type or "").strip().lower()
    if connector_type == "jira":
        project_key = str(config_json.get("project_key") or "").strip()
        return f"Project key {project_key}" if project_key else "Project key not configured"
    if connector_type == "google_drive":
        targets = _normalized_string_list(config_json.get("targets"))
        return f"{len(targets)} target{'s' if len(targets) != 1 else ''}" if targets else "No targets configured"
    if connector_type == "discord":
        channels = _normalized_string_list(config_json.get("channel_ids"))
        threads = _normalized_string_list(config_json.get("thread_ids"))
        parts: list[str] = []
        if channels:
            parts.append(f"{len(channels)} channel{'s' if len(channels) != 1 else ''}")
        if threads:
            parts.append(f"{len(threads)} thread{'s' if len(threads) != 1 else ''}")
        return " · ".join(parts) if parts else "No channels or threads configured"
    return "Configured"


def connector_supports_sync_now(connector_type: str) -> bool:
    return _normalize_connector_type(connector_type) in SYNCABLE_KNOWLEDGE_CONNECTORS


def connector_supports_scheduled_sync(connector_type: str) -> bool:
    return _normalize_connector_type(connector_type) in SCHEDULED_KNOWLEDGE_CONNECTORS


def _normalize_connector_type(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in SUPPORTED_KNOWLEDGE_CONNECTORS:
        raise KnowledgeSourceValidationError(
            f"Unsupported knowledge connector '{value}'. Supported connectors: {', '.join(sorted(SUPPORTED_KNOWLEDGE_CONNECTORS))}"
        )
    return normalized


def _normalize_status(value: str | None) -> str:
    normalized = str(value or "active").strip().lower()
    if normalized not in SOURCE_STATUSES:
        raise KnowledgeSourceValidationError("Knowledge source status must be 'active' or 'disabled'")
    return normalized


def _normalize_sync_mode(value: str | None, *, connector_type: str) -> str:
    normalized = str(value or ("scheduled" if connector_type == "jira" else "manual")).strip().lower()
    if normalized not in SOURCE_SYNC_MODES:
        raise KnowledgeSourceValidationError("Knowledge source sync_mode must be 'manual' or 'scheduled'")
    if normalized == "scheduled" and connector_type not in SCHEDULED_KNOWLEDGE_CONNECTORS:
        raise KnowledgeSourceValidationError(
            f"Connector '{connector_type}' does not support scheduled sync"
        )
    return normalized


def _normalize_config(*, connector_type: str, project: Project, config_json: dict) -> dict:
    if connector_type == "jira":
        project_key = str(config_json.get("project_key") or project.jira_project_key or "").strip().upper()
        if not project_key:
            raise KnowledgeSourceValidationError("Jira knowledge source requires a project key")
        return {"project_key": project_key}
    if connector_type == "google_drive":
        targets = _normalized_string_list(config_json.get("targets"))
        if not targets:
            raise KnowledgeSourceValidationError("Google Drive source requires at least one document, file, or folder target")
        return {"targets": targets}
    if connector_type == "discord":
        channel_ids = _normalized_string_list(config_json.get("channel_ids"))
        thread_ids = _normalized_string_list(config_json.get("thread_ids"))
        if not channel_ids and not thread_ids:
            raise KnowledgeSourceValidationError("Discord source requires at least one channel_id or thread_id")
        return {"channel_ids": channel_ids, "thread_ids": thread_ids}
    raise KnowledgeSourceValidationError(f"Unsupported knowledge connector '{connector_type}'")


def _default_display_name(connector_type: str, config_json: dict) -> str:
    if connector_type == "jira":
        project_key = str(config_json.get("project_key") or "").strip()
        return f"Jira {project_key}" if project_key else "Jira"
    if connector_type == "google_drive":
        return "Google Drive"
    if connector_type == "discord":
        return "Discord"
    return connector_type.replace("_", " ").title()


def _normalized_string_list(value: object) -> list[str]:
    if isinstance(value, str):
        items = value.splitlines()
    elif isinstance(value, (list, tuple, set)):
        items = [str(item) for item in value]
    else:
        items = []
    normalized: list[str] = []
    seen: set[str] = set()
    for item in items:
        candidate = str(item or "").strip()
        if not candidate or candidate in seen:
            continue
        normalized.append(candidate)
        seen.add(candidate)
    return normalized
