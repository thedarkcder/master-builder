from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from orchestrator.api.dependencies import get_session
from orchestrator.api.schemas import (
    KnowledgeAssetCreate,
    KnowledgeAssetRead,
    KnowledgeSyncResultRead,
)
from orchestrator.core.config import get_settings
from orchestrator.core.knowledge_base import (
    create_knowledge_asset,
    decode_base64_content,
    delete_knowledge_asset,
    list_knowledge_assets,
    parse_source_timestamp,
    sync_project_knowledge_from_jira,
)
from orchestrator.core.security import require_admin
from orchestrator.storage.models import JiraOAuthConnection, Project, Tenant
from orchestrator.api.admin.route_helpers import (
    jira_oauth_client,
    refresh_jira_connection_tokens,
)

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _project_for_tenant_or_404(*, session: Session, tenant_id: str, project_id: str) -> Project:
    project = session.get(Project, project_id)
    if project is None or project.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return project


def _knowledge_asset_to_schema(asset) -> KnowledgeAssetRead:  # noqa: ANN001
    return KnowledgeAssetRead(
        asset_id=asset.asset_id,
        tenant_id=asset.tenant_id,
        project_id=asset.project_id,
        source_type=asset.source_type,
        title=asset.title,
        mime_type=asset.mime_type,
        source_ref=asset.source_ref,
        source_timestamp=asset.source_timestamp,
        chunk_count=int(asset.chunk_count or 0),
        status=asset.status,
        created_at=asset.created_at,
        updated_at=asset.updated_at,
    )


@router.get(
    "/tenants/{tenant_id}/projects/{project_id}/knowledge/assets",
    response_model=list[KnowledgeAssetRead],
)
def list_project_knowledge_assets(
    tenant_id: str,
    project_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> list[KnowledgeAssetRead]:
    _project_for_tenant_or_404(session=session, tenant_id=tenant_id, project_id=project_id)
    assets = list_knowledge_assets(session=session, tenant_id=tenant_id, project_id=project_id)
    return [_knowledge_asset_to_schema(asset) for asset in assets]


@router.post(
    "/tenants/{tenant_id}/projects/{project_id}/knowledge/assets",
    response_model=KnowledgeAssetRead,
    status_code=status.HTTP_201_CREATED,
)
def create_project_knowledge_asset(
    tenant_id: str,
    project_id: str,
    payload: KnowledgeAssetCreate,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> KnowledgeAssetRead:
    _project_for_tenant_or_404(session=session, tenant_id=tenant_id, project_id=project_id)
    binary_content = decode_base64_content(payload.content_base64)
    source_timestamp = parse_source_timestamp(payload.source_timestamp)
    asset = create_knowledge_asset(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        source_type=payload.source_type,
        title=payload.title,
        mime_type=payload.mime_type,
        source_ref=payload.source_ref,
        source_timestamp=source_timestamp,
        text_content=payload.text_content,
        binary_content=binary_content,
    )
    return _knowledge_asset_to_schema(asset)


@router.delete(
    "/tenants/{tenant_id}/projects/{project_id}/knowledge/assets/{asset_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_project_knowledge_asset(
    tenant_id: str,
    project_id: str,
    asset_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> Response:
    _project_for_tenant_or_404(session=session, tenant_id=tenant_id, project_id=project_id)
    deleted = delete_knowledge_asset(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        asset_id=asset_id,
    )
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Knowledge asset not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/tenants/{tenant_id}/projects/{project_id}/knowledge/sync-jira",
    response_model=KnowledgeSyncResultRead,
)
def sync_project_knowledge_from_jira_route(
    tenant_id: str,
    project_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> KnowledgeSyncResultRead:
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    project = _project_for_tenant_or_404(session=session, tenant_id=tenant_id, project_id=project_id)
    connection_id = str((tenant.jira_config or {}).get("connection_id") or "").strip()
    if not connection_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Tenant Jira connection is not configured",
        )
    connection = session.get(JiraOAuthConnection, connection_id)
    if connection is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Jira OAuth connection was not found",
        )
    settings = get_settings()
    access_token = refresh_jira_connection_tokens(
        session,
        connection=connection,
        settings=settings,
        tenant_id=tenant_id,
    )
    jira_client = jira_oauth_client(
        session=session,
        settings=settings,
        tenant_id=tenant_id,
        project_id=project_id,
    )
    synced, skipped = sync_project_knowledge_from_jira(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        project_key=project.jira_project_key,
        jira_client=jira_client,
        access_token=access_token,
        cloud_id=connection.cloud_id,
    )
    return KnowledgeSyncResultRead(
        ok=True,
        synced_assets=synced,
        skipped_assets=skipped,
        details=f"Processed Jira project {project.jira_project_key}",
    )
