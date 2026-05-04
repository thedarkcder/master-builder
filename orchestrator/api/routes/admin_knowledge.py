from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.api.dependencies import get_session
from orchestrator.api.schemas import (
    KnowledgeAssetCreate,
    KnowledgeAssetDetailRead,
    KnowledgeAssetPageRead,
    KnowledgeAssetRead,
    KnowledgeDebugMatchRead,
    KnowledgeDebugSearchRead,
    KnowledgeFactRead,
    KnowledgeAssetStatsRead,
    KnowledgeAssetStatusUpdate,
    KnowledgeChunkPageRead,
    KnowledgeChunkRead,
    KnowledgeSourceCreate,
    KnowledgeSourcePageRead,
    KnowledgeSourceRead,
    KnowledgeSourceUpdate,
    KnowledgeSyncResultRead,
)
from orchestrator.core.config import get_settings
from orchestrator.core.decision.types import JiraConfigKey, tenant_jira_config_text
from orchestrator.core.knowledge.base import (
    create_knowledge_asset,
    decode_base64_content,
    delete_knowledge_asset,
    get_knowledge_asset_stats,
    list_knowledge_assets_page,
    list_knowledge_chunks_page,
    list_knowledge_facts_for_asset,
    parse_source_timestamp,
    search_knowledge_debug,
    sync_knowledge_fact_approval_state_for_asset,
    sync_project_knowledge_from_jira,
)
from orchestrator.core.knowledge.sources import (
    KnowledgeSourceSyncUnsupportedError,
    KnowledgeSourceValidationError,
    build_knowledge_source_summary,
    connector_supports_scheduled_sync,
    connector_supports_sync_now,
    create_project_knowledge_source,
    delete_project_knowledge_source,
    get_project_knowledge_source,
    list_project_knowledge_sources,
    sync_project_knowledge_source,
    update_project_knowledge_source,
)
from orchestrator.core.security import require_admin
from orchestrator.storage.models import AtlassianOAuthConnection, KnowledgeAsset, KnowledgeSource, Project, Tenant
from orchestrator.api.admin.route_helpers import (
    atlassian_oauth_client,
    refresh_atlassian_connection_tokens,
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


def _knowledge_asset_to_detail_schema(asset) -> KnowledgeAssetDetailRead:  # noqa: ANN001
    return KnowledgeAssetDetailRead(
        **_knowledge_asset_to_schema(asset).model_dump(),
        text_content=asset.text_content,
        metadata_json=dict(asset.metadata_json or {}),
        facts=[],
    )


def _knowledge_chunk_to_schema(chunk) -> KnowledgeChunkRead:  # noqa: ANN001
    return KnowledgeChunkRead(
        chunk_id=chunk.chunk_id,
        asset_id=chunk.asset_id,
        tenant_id=chunk.tenant_id,
        project_id=chunk.project_id,
        chunk_index=int(chunk.chunk_index or 0),
        content=chunk.content,
        token_count=int(chunk.token_count or 0),
        source_timestamp=chunk.source_timestamp,
        created_at=chunk.created_at,
        updated_at=chunk.updated_at,
    )


def _knowledge_fact_to_schema(fact) -> KnowledgeFactRead:  # noqa: ANN001
    return KnowledgeFactRead(
        fact_id=fact.fact_id,
        asset_id=fact.asset_id,
        chunk_id=fact.chunk_id,
        tenant_id=fact.tenant_id,
        project_id=fact.project_id,
        fact_type=str(getattr(fact, "fact_type", "") or "decision_slot"),
        fact_key=str(getattr(fact, "fact_key", "") or getattr(fact, "slot_name", "") or ""),
        fact_value=str(getattr(fact, "fact_value", "") or getattr(fact, "slot_value", "") or ""),
        approval_state=str(getattr(fact, "approval_state", "") or "approved"),
        confidence=float(getattr(fact, "confidence", 0.0) or 0.0),
        is_inferred=bool(getattr(fact, "is_inferred", False)),
        metadata_json=dict(getattr(fact, "metadata_json", {}) or {}),
        source_timestamp=fact.source_timestamp,
        superseded_at=getattr(fact, "superseded_at", None),
        created_at=fact.created_at,
        updated_at=fact.updated_at,
    )


def _knowledge_source_to_schema(source: KnowledgeSource) -> KnowledgeSourceRead:
    return KnowledgeSourceRead(
        source_id=source.source_id,
        tenant_id=source.tenant_id,
        project_id=source.project_id,
        connector_type=source.connector_type,
        display_name=source.display_name,
        status=source.status,
        sync_mode=source.sync_mode,
        config_json=dict(source.config_json or {}),
        config_summary=build_knowledge_source_summary(source=source),
        supports_sync_now=connector_supports_sync_now(source.connector_type),
        supports_scheduled_sync=connector_supports_scheduled_sync(source.connector_type),
        last_synced_at=source.last_synced_at,
        last_error=source.last_error,
        created_at=source.created_at,
        updated_at=source.updated_at,
    )


def _knowledge_asset_for_project_or_404(
    *,
    session: Session,
    tenant_id: str,
    project_id: str,
    asset_id: str,
) -> KnowledgeAsset:
    asset = session.get(KnowledgeAsset, asset_id)
    if (
        asset is None
        or asset.tenant_id != tenant_id
        or asset.project_id != project_id
        or str(asset.status or "").strip() == "deleted"
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Knowledge asset not found")
    return asset


def _knowledge_source_for_project_or_404(
    *,
    session: Session,
    tenant_id: str,
    project_id: str,
    source_id: str,
) -> KnowledgeSource:
    source = get_project_knowledge_source(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        source_id=source_id,
    )
    if source is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Knowledge source not found")
    return source


@router.get(
    "/tenants/{tenant_id}/projects/{project_id}/knowledge/sources",
    response_model=KnowledgeSourcePageRead,
)
def list_project_knowledge_sources_route(
    tenant_id: str,
    project_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> KnowledgeSourcePageRead:
    _project_for_tenant_or_404(session=session, tenant_id=tenant_id, project_id=project_id)
    items = list_project_knowledge_sources(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
    )
    return KnowledgeSourcePageRead(
        items=[_knowledge_source_to_schema(source) for source in items],
        total=len(items),
    )


@router.post(
    "/tenants/{tenant_id}/projects/{project_id}/knowledge/sources",
    response_model=KnowledgeSourceRead,
    status_code=status.HTTP_201_CREATED,
)
def create_project_knowledge_source_route(
    tenant_id: str,
    project_id: str,
    payload: KnowledgeSourceCreate,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> KnowledgeSourceRead:
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    project = _project_for_tenant_or_404(session=session, tenant_id=tenant_id, project_id=project_id)
    try:
        source = create_project_knowledge_source(
            session=session,
            tenant=tenant,
            project=project,
            connector_type=payload.connector_type,
            display_name=payload.display_name,
            status=payload.status,
            sync_mode=payload.sync_mode,
            config_json=dict(payload.config_json or {}),
        )
    except KnowledgeSourceValidationError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return _knowledge_source_to_schema(source)


@router.patch(
    "/tenants/{tenant_id}/projects/{project_id}/knowledge/sources/{source_id}",
    response_model=KnowledgeSourceRead,
)
def update_project_knowledge_source_route(
    tenant_id: str,
    project_id: str,
    source_id: str,
    payload: KnowledgeSourceUpdate,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> KnowledgeSourceRead:
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    project = _project_for_tenant_or_404(session=session, tenant_id=tenant_id, project_id=project_id)
    source = _knowledge_source_for_project_or_404(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        source_id=source_id,
    )
    try:
        source = update_project_knowledge_source(
            session=session,
            tenant=tenant,
            project=project,
            source=source,
            display_name=payload.display_name,
            status=payload.status,
            sync_mode=payload.sync_mode,
            config_json=dict(payload.config_json) if payload.config_json is not None else None,
        )
    except KnowledgeSourceValidationError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return _knowledge_source_to_schema(source)


@router.delete(
    "/tenants/{tenant_id}/projects/{project_id}/knowledge/sources/{source_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_project_knowledge_source_route(
    tenant_id: str,
    project_id: str,
    source_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> Response:
    _project_for_tenant_or_404(session=session, tenant_id=tenant_id, project_id=project_id)
    source = _knowledge_source_for_project_or_404(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        source_id=source_id,
    )
    delete_project_knowledge_source(session=session, source=source)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/tenants/{tenant_id}/projects/{project_id}/knowledge/sources/{source_id}/sync",
    response_model=KnowledgeSyncResultRead,
)
def sync_project_knowledge_source_route(
    tenant_id: str,
    project_id: str,
    source_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> KnowledgeSyncResultRead:
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    project = _project_for_tenant_or_404(session=session, tenant_id=tenant_id, project_id=project_id)
    source = _knowledge_source_for_project_or_404(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        source_id=source_id,
    )
    settings = get_settings()
    try:
        sync_result = sync_project_knowledge_source(
            session=session,
            tenant=tenant,
            project=project,
            source=source,
            settings=settings,
        )
    except KnowledgeSourceValidationError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except KnowledgeSourceSyncUnsupportedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return KnowledgeSyncResultRead(
        ok=sync_result.ok,
        synced_assets=sync_result.synced_assets,
        skipped_assets=sync_result.skipped_assets + sync_result.unchanged_assets,
        created_assets=sync_result.created_assets,
        updated_assets=sync_result.updated_assets,
        unchanged_assets=sync_result.unchanged_assets,
        deleted_assets=sync_result.deleted_assets,
        failed_assets=sync_result.failed_assets,
        details=sync_result.details or f"Processed {source.connector_type} source {source.display_name}",
    )


@router.get(
    "/tenants/{tenant_id}/projects/{project_id}/knowledge/assets",
    response_model=KnowledgeAssetPageRead,
)
def list_project_knowledge_assets(
    tenant_id: str,
    project_id: str,
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    status_filter: str | None = Query(default=None, alias="status"),
    source_type: str | None = Query(default=None),
    query: str | None = Query(default=None, alias="q"),
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> KnowledgeAssetPageRead:
    _project_for_tenant_or_404(session=session, tenant_id=tenant_id, project_id=project_id)
    assets, total = list_knowledge_assets_page(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        status=status_filter,
        source_type=source_type,
        query=query,
        limit=limit,
        offset=offset,
    )
    return KnowledgeAssetPageRead(
        items=[_knowledge_asset_to_schema(asset) for asset in assets],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/tenants/{tenant_id}/projects/{project_id}/knowledge/stats",
    response_model=KnowledgeAssetStatsRead,
)
def get_project_knowledge_stats(
    tenant_id: str,
    project_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> KnowledgeAssetStatsRead:
    _project_for_tenant_or_404(session=session, tenant_id=tenant_id, project_id=project_id)
    return KnowledgeAssetStatsRead(
        **get_knowledge_asset_stats(session=session, tenant_id=tenant_id, project_id=project_id)
    )


@router.get(
    "/tenants/{tenant_id}/projects/{project_id}/knowledge/assets/{asset_id}",
    response_model=KnowledgeAssetDetailRead,
)
def get_project_knowledge_asset(
    tenant_id: str,
    project_id: str,
    asset_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> KnowledgeAssetDetailRead:
    _project_for_tenant_or_404(session=session, tenant_id=tenant_id, project_id=project_id)
    asset = _knowledge_asset_for_project_or_404(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        asset_id=asset_id,
    )
    detail = _knowledge_asset_to_detail_schema(asset)
    detail.facts = [
        _knowledge_fact_to_schema(fact)
        for fact in list_knowledge_facts_for_asset(
            session=session,
            tenant_id=tenant_id,
            project_id=project_id,
            asset_id=asset_id,
        )
    ]
    return detail


@router.get(
    "/tenants/{tenant_id}/projects/{project_id}/knowledge/assets/{asset_id}/chunks",
    response_model=KnowledgeChunkPageRead,
)
def list_project_knowledge_chunks(
    tenant_id: str,
    project_id: str,
    asset_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> KnowledgeChunkPageRead:
    _project_for_tenant_or_404(session=session, tenant_id=tenant_id, project_id=project_id)
    _knowledge_asset_for_project_or_404(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        asset_id=asset_id,
    )
    chunks, total = list_knowledge_chunks_page(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        asset_id=asset_id,
        limit=limit,
        offset=offset,
    )
    return KnowledgeChunkPageRead(
        items=[_knowledge_chunk_to_schema(chunk) for chunk in chunks],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/tenants/{tenant_id}/projects/{project_id}/knowledge/debug-search",
    response_model=KnowledgeDebugSearchRead,
)
def debug_project_knowledge_search(
    tenant_id: str,
    project_id: str,
    query: str = Query(min_length=1),
    limit: int = Query(default=5, ge=1, le=20),
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> KnowledgeDebugSearchRead:
    _project_for_tenant_or_404(session=session, tenant_id=tenant_id, project_id=project_id)
    matches = search_knowledge_debug(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        query=query,
        limit=limit,
    )
    return KnowledgeDebugSearchRead(
        query=query,
        items=[
            KnowledgeDebugMatchRead(
                layer=item.layer,
                score=item.score,
                asset_id=item.asset_id,
                source_type=item.source_type,
                title=item.title,
                source_ref=item.source_ref,
                source_timestamp=item.source_timestamp,
                fact_id=item.fact_id,
                chunk_id=item.chunk_id,
                snippet=item.snippet,
                metadata=dict(item.metadata or {}),
            )
            for item in matches
        ],
    )


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


@router.patch(
    "/tenants/{tenant_id}/projects/{project_id}/knowledge/assets/{asset_id}/status",
    response_model=KnowledgeAssetRead,
)
def update_project_knowledge_asset_status(
    tenant_id: str,
    project_id: str,
    asset_id: str,
    payload: KnowledgeAssetStatusUpdate,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> KnowledgeAssetRead:
    _project_for_tenant_or_404(session=session, tenant_id=tenant_id, project_id=project_id)
    asset = _knowledge_asset_for_project_or_404(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        asset_id=asset_id,
    )
    next_status = str(payload.status or "").strip()
    allowed_transitions = {
        "pending_review": {"ready", "rejected"},
        "rejected": {"pending_review"},
    }
    current_status = str(asset.status or "").strip()
    if current_status == next_status:
        return _knowledge_asset_to_schema(asset)
    if next_status not in allowed_transitions.get(current_status, set()):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Knowledge asset status cannot transition from '{current_status}' to '{next_status}'",
        )
    asset.status = next_status
    asset.updated_at = datetime.now(timezone.utc)
    sync_knowledge_fact_approval_state_for_asset(session=session, asset=asset)
    session.commit()
    session.refresh(asset)
    return _knowledge_asset_to_schema(asset)


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
    source = session.execute(
        select(KnowledgeSource)
        .where(
            KnowledgeSource.tenant_id == tenant_id,
            KnowledgeSource.project_id == project_id,
            KnowledgeSource.connector_type == "jira",
        )
        .order_by(KnowledgeSource.created_at.asc())
    ).scalar_one_or_none()
    if source is not None:
        return sync_project_knowledge_source_route(
            tenant_id=tenant_id,
            project_id=project_id,
            source_id=source.source_id,
            _="admin",
            session=session,
        )

    connection_id = tenant_jira_config_text(tenant=tenant, key=JiraConfigKey.CONNECTION_ID)
    if not connection_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Tenant Atlassian connection is not configured")
    connection = session.get(AtlassianOAuthConnection, connection_id)
    if connection is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Atlassian connection was not found")
    access_token = refresh_atlassian_connection_tokens(
        session,
        connection=connection,
        settings=get_settings(),
        tenant_id=tenant_id,
    )
    jira_client = atlassian_oauth_client(session=session, settings=get_settings(), tenant_id=tenant_id, project_id=project_id)
    sync_result = sync_project_knowledge_from_jira(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        project_key=project.jira_project_key,
        jira_client=jira_client,
        access_token=access_token,
        cloud_id=connection.cloud_id,
    )
    return KnowledgeSyncResultRead(
        ok=sync_result.ok,
        synced_assets=sync_result.synced_assets,
        skipped_assets=sync_result.skipped_assets + sync_result.unchanged_assets,
        created_assets=sync_result.created_assets,
        updated_assets=sync_result.updated_assets,
        unchanged_assets=sync_result.unchanged_assets,
        deleted_assets=sync_result.deleted_assets,
        failed_assets=sync_result.failed_assets,
        details=sync_result.details or f"Processed Jira project {project.jira_project_key}",
    )
