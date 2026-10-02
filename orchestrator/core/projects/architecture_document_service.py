from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.api.atlassian_oauth.connection_service import (
    tenant_atlassian_oauth_context,
)
from orchestrator.core.knowledge.base import (
    create_knowledge_asset,
    replace_knowledge_asset_text,
)
from orchestrator.storage.models import (
    ArchitectureDocument,
    KnowledgeAsset,
    Project,
    Tenant,
)
from orchestrator.tools.atlassian_oauth import AtlassianOAuthError
from orchestrator.tools.atlassian_oauth_confluence_service import (
    confluence_storage_template,
)

ARCHITECTURE_DOC_PROVIDER_INTERNAL = "internal"
ARCHITECTURE_DOC_PROVIDER_CONFLUENCE = "confluence"
ARCHITECTURE_DOC_STATUS_DRAFT = "draft"
ARCHITECTURE_DOC_STATUS_READY = "ready"
ARCHITECTURE_DOC_STATUS_SUPERSEDED = "superseded"
ARCHITECTURE_REQUIRED_LABEL = "architecture-required"
ARCHITECTURE_DOC_SOURCE_TYPE = "architecture_document"
CONFLUENCE_REQUIRED_SCOPES = frozenset(
    {"read:space:confluence", "write:page:confluence"}
)


@dataclass(frozen=True)
class ArchitectureDocumentGate:
    required: bool
    provider: str | None
    document: ArchitectureDocument | None
    ready: bool
    block_reason: str | None = None


@dataclass(frozen=True)
class ArchitectureDocumentLink:
    title: str
    url: str


def architecture_required_for_issue(
    *, issue_labels: list[str] | tuple[str, ...]
) -> bool:
    normalized = {str(label or "").strip().casefold() for label in issue_labels}
    return ARCHITECTURE_REQUIRED_LABEL in normalized


def architecture_docs_provider_for_project(*, project: Project) -> str | None:
    config = dict(getattr(project, "architecture_docs_config", {}) or {})
    provider = str(config.get("provider") or "").strip().lower()
    if provider in {
        ARCHITECTURE_DOC_PROVIDER_INTERNAL,
        ARCHITECTURE_DOC_PROVIDER_CONFLUENCE,
    }:
        return provider
    return None


def _default_document_title(*, parent_issue_key: str, issue_summary: str) -> str:
    normalized_summary = str(issue_summary or "").strip() or "Architecture"
    return f"{parent_issue_key}: {normalized_summary[:220]}"


def _internal_document_url(
    *, admin_ui_base_url: str, tenant_id: str, project_id: str, document_id: str
) -> str:
    base = str(admin_ui_base_url or "").strip().rstrip("/")
    if not base:
        raise ValueError(
            "admin_ui_base_url is required for internal architecture documents"
        )
    return (
        f"{base}/{quote(tenant_id, safe='')}/projects/{quote(project_id, safe='')}"
        f"/architecture?documentId={quote(document_id, safe='')}"
    )


def _internal_document_template(
    *, title: str, parent_issue_key: str, issue_summary: str
) -> str:
    normalized_summary = (
        str(issue_summary or "").strip() or "Add the parent epic summary here."
    )
    return "\n".join(
        [
            f"# {title}",
            "",
            f"- Parent issue: {parent_issue_key}",
            f"- Epic summary: {normalized_summary}",
            "",
            "## Architecture Overview",
            "- Describe the system-level design for this epic.",
            "",
            "## System Diagrams",
            "- Add the current system and integration diagrams.",
            "",
            "## Data Models",
            "- Document the data structures and schema changes.",
            "",
            "## Key Decisions (ADRs)",
            "- Record the architectural decisions and their trade-offs.",
            "",
            "## API Contracts",
            "- Capture the high-level contracts and integration boundaries.",
            "",
            "## Constraints",
            "- List rollout, migration, or dependency constraints.",
        ]
    ).strip()


def _confluence_config_for_project(*, project: Project) -> tuple[str, str | None]:
    config = dict(getattr(project, "architecture_docs_config", {}) or {})
    space_key = str(config.get("space_key") or "").strip()
    parent_page_id = str(config.get("parent_page_id") or "").strip() or None
    if not space_key:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Project {project.project_id} is configured for Confluence architecture docs but has no space key",
        )
    return space_key, parent_page_id


def _tenant_for_project_or_404(*, session: Session, project: Project) -> Tenant:
    tenant = session.get(Tenant, project.tenant_id)
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found for project"
        )
    return tenant


def _tenant_by_id_or_404(*, session: Session, tenant_id: str) -> Tenant:
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found"
        )
    return tenant


class ArchitectureDocumentService:
    def __init__(self, *, settings_factory) -> None:  # noqa: ANN001
        self._settings_factory = settings_factory

    def _create_internal_document(
        self,
        *,
        session: Session,
        project: Project,
        parent_issue_key: str,
        issue_summary: str,
        actor: str | None,
        title: str | None = None,
    ) -> ArchitectureDocument:
        now = datetime.now(timezone.utc)
        document_id = uuid4().hex
        document_title = str(title or "").strip() or _default_document_title(
            parent_issue_key=parent_issue_key,
            issue_summary=issue_summary,
        )
        canonical_url = _internal_document_url(
            admin_ui_base_url=self._settings_factory().admin_ui_base_url,
            tenant_id=project.tenant_id,
            project_id=project.project_id,
            document_id=document_id,
        )
        asset = create_knowledge_asset(
            session=session,
            tenant_id=project.tenant_id,
            project_id=project.project_id,
            source_type=ARCHITECTURE_DOC_SOURCE_TYPE,
            title=document_title,
            mime_type="text/markdown",
            source_ref=f"architecture-document:{parent_issue_key}",
            text_content=_internal_document_template(
                title=document_title,
                parent_issue_key=parent_issue_key,
                issue_summary=issue_summary,
            ),
            metadata_json={
                "document_id": document_id,
                "parent_issue_key": str(parent_issue_key or "").strip().upper(),
                "provider": ARCHITECTURE_DOC_PROVIDER_INTERNAL,
            },
            status="pending_review",
            commit=False,
        )
        document = ArchitectureDocument(
            document_id=document_id,
            tenant_id=project.tenant_id,
            project_id=project.project_id,
            parent_issue_key=str(parent_issue_key or "").strip().upper(),
            provider=ARCHITECTURE_DOC_PROVIDER_INTERNAL,
            title=document_title,
            status=ARCHITECTURE_DOC_STATUS_DRAFT,
            is_active=True,
            canonical_url=canonical_url,
            provider_ref=asset.asset_id,
            knowledge_asset_id=asset.asset_id,
            metadata_json={},
            created_by=actor,
            updated_by=actor,
            created_at=now,
            updated_at=now,
        )
        session.add(document)
        session.commit()
        session.refresh(document)
        return document

    def _create_confluence_document(
        self,
        *,
        session: Session,
        project: Project,
        parent_issue_key: str,
        issue_summary: str,
        actor: str | None,
        title: str | None = None,
    ) -> ArchitectureDocument:
        tenant = _tenant_for_project_or_404(session=session, project=project)
        space_key, parent_page_id = _confluence_config_for_project(project=project)
        settings = self._settings_factory()
        oauth = tenant_atlassian_oauth_context(
            session=session, tenant=tenant, settings=settings
        )
        granted_scopes = {
            str(scope or "").strip()
            for scope in getattr(oauth.connection, "scopes", [])
            if str(scope or "").strip()
        }
        missing_scopes = sorted(CONFLUENCE_REQUIRED_SCOPES - granted_scopes)
        if missing_scopes:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Connected Atlassian OAuth grant is missing required Confluence scopes: "
                    + ", ".join(missing_scopes)
                    + ". Reconnect the Atlassian integration with Confluence access."
                ),
            )
        document_title = str(title or "").strip() or _default_document_title(
            parent_issue_key=parent_issue_key,
            issue_summary=issue_summary,
        )
        try:
            space = oauth.client.get_confluence_space_by_key(
                access_token=oauth.access_token,
                cloud_id=oauth.connection.cloud_id,
                space_key=space_key,
            )
            page = oauth.client.create_confluence_page(
                access_token=oauth.access_token,
                cloud_id=oauth.connection.cloud_id,
                site_url=oauth.connection.site_url,
                space_id=space.space_id,
                title=document_title,
                body_storage_value=confluence_storage_template(
                    title=document_title,
                    parent_issue_key=parent_issue_key,
                    issue_summary=issue_summary,
                ),
                parent_page_id=parent_page_id,
            )
        except AtlassianOAuthError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Unable to create Confluence architecture document: {exc}",
            ) from exc
        now = datetime.now(timezone.utc)
        document = ArchitectureDocument(
            document_id=uuid4().hex,
            tenant_id=project.tenant_id,
            project_id=project.project_id,
            parent_issue_key=str(parent_issue_key or "").strip().upper(),
            provider=ARCHITECTURE_DOC_PROVIDER_CONFLUENCE,
            title=page.title,
            status=ARCHITECTURE_DOC_STATUS_DRAFT,
            is_active=True,
            canonical_url=page.webui_url,
            provider_ref=page.page_id,
            knowledge_asset_id=None,
            metadata_json={
                "space_key": space.key,
                "space_id": space.space_id,
                "parent_page_id": parent_page_id,
            },
            created_by=actor,
            updated_by=actor,
            created_at=now,
            updated_at=now,
        )
        session.add(document)
        session.commit()
        session.refresh(document)
        return document

    def get_active_document(
        self,
        *,
        session: Session,
        tenant_id: str,
        project_id: str,
        parent_issue_key: str,
    ) -> ArchitectureDocument | None:
        normalized_parent_issue_key = str(parent_issue_key or "").strip().upper()
        if not normalized_parent_issue_key:
            return None
        return (
            session.execute(
                select(ArchitectureDocument).where(
                    ArchitectureDocument.tenant_id == tenant_id,
                    ArchitectureDocument.project_id == project_id,
                    ArchitectureDocument.parent_issue_key
                    == normalized_parent_issue_key,
                    ArchitectureDocument.is_active.is_(True),
                )
            )
            .scalars()
            .first()
        )

    def ensure_document_for_issue(
        self,
        *,
        session: Session,
        project: Project,
        parent_issue_key: str,
        issue_summary: str,
        issue_labels: list[str] | tuple[str, ...],
        actor: str | None,
    ) -> ArchitectureDocument | None:
        if not architecture_required_for_issue(issue_labels=issue_labels):
            return None
        provider = architecture_docs_provider_for_project(project=project)
        if provider is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Jira issue {parent_issue_key} is labeled '{ARCHITECTURE_REQUIRED_LABEL}' "
                    "but the project has no architecture document provider configured"
                ),
            )
        existing = self.get_active_document(
            session=session,
            tenant_id=project.tenant_id,
            project_id=project.project_id,
            parent_issue_key=parent_issue_key,
        )
        if existing is not None:
            return existing
        if provider == ARCHITECTURE_DOC_PROVIDER_INTERNAL:
            return self._create_internal_document(
                session=session,
                project=project,
                parent_issue_key=parent_issue_key,
                issue_summary=issue_summary,
                actor=actor,
            )
        if provider == ARCHITECTURE_DOC_PROVIDER_CONFLUENCE:
            return self._create_confluence_document(
                session=session,
                project=project,
                parent_issue_key=parent_issue_key,
                issue_summary=issue_summary,
                actor=actor,
            )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Unsupported architecture document provider '{provider}'",
        )

    def resolve_gate(
        self,
        *,
        session: Session,
        project: Project,
        parent_issue_key: str,
        issue_summary: str,
        issue_labels: list[str] | tuple[str, ...],
        actor: str | None,
    ) -> ArchitectureDocumentGate:
        if not architecture_required_for_issue(issue_labels=issue_labels):
            return ArchitectureDocumentGate(
                required=False, provider=None, document=None, ready=True
            )
        provider = architecture_docs_provider_for_project(project=project)
        if provider is None:
            return ArchitectureDocumentGate(
                required=True,
                provider=None,
                document=None,
                ready=False,
                block_reason=(
                    f"Project {project.project_id} has no architecture document provider configured "
                    f"for '{ARCHITECTURE_REQUIRED_LABEL}' epics"
                ),
            )
        document = self.ensure_document_for_issue(
            session=session,
            project=project,
            parent_issue_key=parent_issue_key,
            issue_summary=issue_summary,
            issue_labels=issue_labels,
            actor=actor,
        )
        if document is None:
            return ArchitectureDocumentGate(
                required=True,
                provider=provider,
                document=None,
                ready=False,
                block_reason=(
                    "Architecture document is required before planning can continue. "
                    "Create or link the document from the project architecture page."
                ),
            )
        if str(document.status or "").strip().lower() != ARCHITECTURE_DOC_STATUS_READY:
            return ArchitectureDocumentGate(
                required=True,
                provider=provider,
                document=document,
                ready=False,
                block_reason=(
                    f"Architecture document '{document.title}' is still draft. "
                    "Mark it ready before planning can continue."
                ),
            )
        return ArchitectureDocumentGate(
            required=True, provider=provider, document=document, ready=True
        )

    def require_document_link(
        self,
        *,
        session: Session,
        project: Project,
        parent_issue_key: str,
        issue_summary: str,
        issue_labels: list[str] | tuple[str, ...],
        actor: str | None,
    ) -> ArchitectureDocumentLink | None:
        gate = self.resolve_gate(
            session=session,
            project=project,
            parent_issue_key=parent_issue_key,
            issue_summary=issue_summary,
            issue_labels=issue_labels,
            actor=actor,
        )
        if not gate.required:
            return None
        document = gate.document
        if document is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=gate.block_reason
                or "Architecture document link is required before planning can continue",
            )
        normalized_url = str(document.canonical_url or "").strip()
        if not normalized_url:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Architecture document '{document.title}' is missing its canonical URL",
            )
        return ArchitectureDocumentLink(title=document.title, url=normalized_url)

    def create_document(
        self,
        *,
        session: Session,
        project: Project,
        parent_issue_key: str,
        issue_summary: str,
        actor: str | None,
        title: str | None = None,
        canonical_url: str | None = None,
        provider_ref: str | None = None,
    ) -> ArchitectureDocument:
        provider = architecture_docs_provider_for_project(project=project)
        if provider is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Project architecture document provider is not configured",
            )
        existing = self.get_active_document(
            session=session,
            tenant_id=project.tenant_id,
            project_id=project.project_id,
            parent_issue_key=parent_issue_key,
        )
        if existing is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="An active architecture document already exists for this epic",
            )
        if provider == ARCHITECTURE_DOC_PROVIDER_INTERNAL:
            if str(canonical_url or "").strip() or str(provider_ref or "").strip():
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Internal architecture documents do not accept provider URLs or external references",
                )
            return self._create_internal_document(
                session=session,
                project=project,
                parent_issue_key=parent_issue_key,
                issue_summary=issue_summary,
                actor=actor,
                title=title,
            )
        if provider == ARCHITECTURE_DOC_PROVIDER_CONFLUENCE:
            if str(canonical_url or "").strip() or str(provider_ref or "").strip():
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Confluence-backed architecture documents are created from project configuration and cannot override the provider URL",
                )
            return self._create_confluence_document(
                session=session,
                project=project,
                parent_issue_key=parent_issue_key,
                issue_summary=issue_summary,
                actor=actor,
                title=title,
            )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Unsupported architecture document provider '{provider}'",
        )

    def get_document_for_project(
        self,
        *,
        session: Session,
        tenant_id: str,
        project_id: str,
        document_id: str,
    ) -> ArchitectureDocument:
        document = session.get(ArchitectureDocument, document_id)
        if (
            document is None
            or document.tenant_id != tenant_id
            or document.project_id != project_id
        ):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Architecture document not found",
            )
        return document

    def list_documents_for_project(
        self,
        *,
        session: Session,
        tenant_id: str,
        project_id: str,
        parent_issue_key: str | None = None,
    ) -> list[ArchitectureDocument]:
        stmt = select(ArchitectureDocument).where(
            ArchitectureDocument.tenant_id == tenant_id,
            ArchitectureDocument.project_id == project_id,
            ArchitectureDocument.is_active.is_(True),
        )
        normalized_parent_issue_key = str(parent_issue_key or "").strip().upper()
        if normalized_parent_issue_key:
            stmt = stmt.where(
                ArchitectureDocument.parent_issue_key == normalized_parent_issue_key
            )
        return (
            session.execute(stmt.order_by(ArchitectureDocument.created_at.desc()))
            .scalars()
            .all()
        )

    def update_document(
        self,
        *,
        session: Session,
        document: ArchitectureDocument,
        title: str,
        status_value: str,
        actor: str | None,
        content_markdown: str | None = None,
        canonical_url: str | None = None,
        provider_ref: str | None = None,
    ) -> ArchitectureDocument:
        normalized_title = str(title or "").strip()
        if not normalized_title:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Architecture document title is required",
            )
        normalized_status = str(status_value or "").strip().lower()
        if normalized_status not in {
            ARCHITECTURE_DOC_STATUS_DRAFT,
            ARCHITECTURE_DOC_STATUS_READY,
            ARCHITECTURE_DOC_STATUS_SUPERSEDED,
        }:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid architecture document status",
            )
        if document.provider == ARCHITECTURE_DOC_PROVIDER_INTERNAL:
            normalized_content = str(content_markdown or "").strip()
            if not normalized_content:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Internal architecture documents require markdown content",
                )
            if not document.knowledge_asset_id:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Internal architecture document is missing its knowledge asset",
                )
            replace_knowledge_asset_text(
                session=session,
                tenant_id=document.tenant_id,
                project_id=document.project_id,
                asset_id=document.knowledge_asset_id,
                title=normalized_title,
                text_content=normalized_content,
                metadata_json={
                    "document_id": document.document_id,
                    "parent_issue_key": document.parent_issue_key,
                    "provider": document.provider,
                },
                status="ready"
                if normalized_status == ARCHITECTURE_DOC_STATUS_READY
                else "pending_review",
                commit=False,
            )
        else:
            if str(content_markdown or "").strip():
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Confluence-backed architecture documents are edited in Confluence, not in the platform markdown editor",
                )
            if str(canonical_url or "").strip() or str(provider_ref or "").strip():
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Confluence-backed architecture documents do not accept manual URL or provider reference overrides",
                )
            normalized_page_id = str(document.provider_ref or "").strip()
            if not normalized_page_id:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Confluence-backed architecture document is missing its page id",
                )
            tenant = _tenant_by_id_or_404(session=session, tenant_id=document.tenant_id)
            settings = self._settings_factory()
            oauth = tenant_atlassian_oauth_context(
                session=session, tenant=tenant, settings=settings
            )
            granted_scopes = {
                str(scope or "").strip()
                for scope in getattr(oauth.connection, "scopes", [])
                if str(scope or "").strip()
            }
            missing_scopes = sorted({"write:page:confluence"} - granted_scopes)
            if missing_scopes:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "Connected Atlassian OAuth grant is missing required Confluence scopes: "
                        + ", ".join(missing_scopes)
                        + ". Reconnect the Atlassian integration with Confluence access."
                    ),
                )
            try:
                updated_page = oauth.client.update_confluence_page_title(
                    access_token=oauth.access_token,
                    cloud_id=oauth.connection.cloud_id,
                    site_url=oauth.connection.site_url,
                    page_id=normalized_page_id,
                    title=normalized_title,
                )
            except AtlassianOAuthError as exc:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"Unable to update Confluence architecture document: {exc}",
                ) from exc
            document.canonical_url = updated_page.webui_url
            document.provider_ref = updated_page.page_id
        document.title = normalized_title
        document.status = normalized_status
        document.updated_by = actor
        document.updated_at = datetime.now(timezone.utc)
        session.commit()
        session.refresh(document)
        return document

    def document_content(
        self, *, session: Session, document: ArchitectureDocument
    ) -> str | None:
        if (
            document.provider != ARCHITECTURE_DOC_PROVIDER_INTERNAL
            or not document.knowledge_asset_id
        ):
            return None
        knowledge_asset = session.get(KnowledgeAsset, document.knowledge_asset_id)
        if knowledge_asset is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Internal architecture document is missing its knowledge asset",
            )
        return str(knowledge_asset.text_content or "")
