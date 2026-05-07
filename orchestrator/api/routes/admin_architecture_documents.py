from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from orchestrator.api.dependencies import get_session
from orchestrator.api.schemas import (
    ArchitectureDocumentCreate,
    ArchitectureDocumentPageRead,
    ArchitectureDocumentRead,
    ArchitectureDocumentUpdate,
)
from orchestrator.core.projects.architecture_document_service import ArchitectureDocumentService
from orchestrator.core.config import get_settings
from orchestrator.core.security import (
    AuthenticatedPrincipal,
    require_authenticated_principal,
    require_tenant_workspace_access,
)
from orchestrator.storage.models import Project

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _project_for_tenant_or_404(*, session: Session, tenant_id: str, project_id: str) -> Project:
    project = session.get(Project, project_id)
    if project is None or project.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return project


def _service() -> ArchitectureDocumentService:
    return ArchitectureDocumentService(settings_factory=get_settings)


def _to_schema(*, service: ArchitectureDocumentService, session: Session, document) -> ArchitectureDocumentRead:  # noqa: ANN001
    return ArchitectureDocumentRead(
        document_id=document.document_id,
        tenant_id=document.tenant_id,
        project_id=document.project_id,
        parent_issue_key=document.parent_issue_key,
        provider=document.provider,
        title=document.title,
        status=document.status,
        is_active=bool(document.is_active),
        canonical_url=document.canonical_url,
        provider_ref=document.provider_ref,
        knowledge_asset_id=document.knowledge_asset_id,
        content_markdown=service.document_content(session=session, document=document),
        metadata=dict(document.metadata_json or {}),
        created_by=document.created_by,
        updated_by=document.updated_by,
        created_at=document.created_at,
        updated_at=document.updated_at,
    )


@router.get(
    "/tenants/{tenant_id}/projects/{project_id}/architecture-documents",
    response_model=ArchitectureDocumentPageRead,
)
def list_project_architecture_documents(
    tenant_id: str,
    project_id: str,
    parent_issue_key: str | None = Query(default=None),
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> ArchitectureDocumentPageRead:
    require_tenant_workspace_access(principal=principal, tenant_id=tenant_id)
    _project_for_tenant_or_404(session=session, tenant_id=tenant_id, project_id=project_id)
    service = _service()
    items = service.list_documents_for_project(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        parent_issue_key=parent_issue_key,
    )
    return ArchitectureDocumentPageRead(
        items=[_to_schema(service=service, session=session, document=item) for item in items],
        total=len(items),
    )


@router.post(
    "/tenants/{tenant_id}/projects/{project_id}/architecture-documents",
    response_model=ArchitectureDocumentRead,
    status_code=status.HTTP_201_CREATED,
)
def create_project_architecture_document(
    tenant_id: str,
    project_id: str,
    payload: ArchitectureDocumentCreate,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> ArchitectureDocumentRead:
    require_tenant_workspace_access(principal=principal, tenant_id=tenant_id)
    project = _project_for_tenant_or_404(session=session, tenant_id=tenant_id, project_id=project_id)
    service = _service()
    document = service.create_document(
        session=session,
        project=project,
        parent_issue_key=payload.parent_issue_key,
        issue_summary=payload.issue_summary or payload.title or payload.parent_issue_key,
        actor=principal.user_id or principal.username,
        title=payload.title,
        canonical_url=payload.canonical_url,
        provider_ref=payload.provider_ref,
    )
    return _to_schema(service=service, session=session, document=document)


@router.get(
    "/tenants/{tenant_id}/projects/{project_id}/architecture-documents/{document_id}",
    response_model=ArchitectureDocumentRead,
)
def get_project_architecture_document(
    tenant_id: str,
    project_id: str,
    document_id: str,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> ArchitectureDocumentRead:
    require_tenant_workspace_access(principal=principal, tenant_id=tenant_id)
    _project_for_tenant_or_404(session=session, tenant_id=tenant_id, project_id=project_id)
    service = _service()
    document = service.get_document_for_project(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        document_id=document_id,
    )
    return _to_schema(service=service, session=session, document=document)


@router.put(
    "/tenants/{tenant_id}/projects/{project_id}/architecture-documents/{document_id}",
    response_model=ArchitectureDocumentRead,
)
def update_project_architecture_document(
    tenant_id: str,
    project_id: str,
    document_id: str,
    payload: ArchitectureDocumentUpdate,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> ArchitectureDocumentRead:
    require_tenant_workspace_access(principal=principal, tenant_id=tenant_id)
    _project_for_tenant_or_404(session=session, tenant_id=tenant_id, project_id=project_id)
    service = _service()
    document = service.get_document_for_project(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        document_id=document_id,
    )
    updated = service.update_document(
        session=session,
        document=document,
        title=payload.title,
        status_value=payload.status,
        actor=principal.user_id or principal.username,
        content_markdown=payload.content_markdown,
        canonical_url=payload.canonical_url,
        provider_ref=payload.provider_ref,
    )
    return _to_schema(service=service, session=session, document=updated)
