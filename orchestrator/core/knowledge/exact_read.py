from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.api.atlassian_oauth.service import (
    atlassian_oauth_client,
    refresh_atlassian_connection_tokens,
)
from orchestrator.core.decision.types import JiraConfigKey, tenant_jira_config_text
from orchestrator.storage.models import (
    AtlassianOAuthConnection,
    KnowledgeAsset,
    Project,
    Tenant,
)


@dataclass(frozen=True)
class ExactReadRequest:
    tenant: Tenant
    project: Project
    issue_key: str
    source_type: str | None = None
    source_ref: str | None = None
    asset_id: str | None = None


def exact_read_knowledge_source(
    *,
    session: Session,
    settings,
    request: ExactReadRequest,
) -> dict[str, Any]:
    asset = _resolve_asset(session=session, request=request)
    if asset is None:
        return {"ok": False, "layer": "exact_read", "reason": "asset_not_found"}
    source_type = str(asset.source_type or "").strip().lower()
    if source_type.startswith("jira_"):
        live_payload = _jira_exact_read(
            session=session,
            settings=settings,
            tenant=request.tenant,
            asset=asset,
        )
        if live_payload is not None:
            return live_payload
    return {
        "ok": True,
        "layer": "exact_read",
        "connector": "stored_asset",
        "source_type": asset.source_type,
        "asset_id": asset.asset_id,
        "source_ref": asset.source_ref,
        "title": asset.title,
        "content": asset.text_content or "",
        "metadata": dict(asset.metadata_json or {}),
        "source_timestamp": asset.source_timestamp.isoformat()
        if asset.source_timestamp
        else None,
    }


def _resolve_asset(
    *, session: Session, request: ExactReadRequest
) -> KnowledgeAsset | None:
    if request.asset_id:
        asset = session.get(KnowledgeAsset, request.asset_id)
        if (
            asset is None
            or asset.tenant_id != request.tenant.tenant_id
            or asset.project_id != request.project.project_id
            or str(asset.status or "").strip() == "deleted"
        ):
            return None
        return asset
    source_type = str(request.source_type or "").strip().lower()
    source_ref = str(request.source_ref or "").strip()
    if not source_type or not source_ref:
        return None
    return session.execute(
        select(KnowledgeAsset).where(
            KnowledgeAsset.tenant_id == request.tenant.tenant_id,
            KnowledgeAsset.project_id == request.project.project_id,
            KnowledgeAsset.source_type == source_type,
            KnowledgeAsset.source_ref == source_ref,
            KnowledgeAsset.status != "deleted",
        )
    ).scalar_one_or_none()


def _jira_exact_read(
    *,
    session: Session,
    settings,
    tenant: Tenant,
    asset: KnowledgeAsset,
) -> dict[str, Any] | None:
    connection_id = tenant_jira_config_text(
        tenant=tenant, key=JiraConfigKey.CONNECTION_ID
    )
    if not connection_id:
        return None
    connection = session.get(AtlassianOAuthConnection, connection_id)
    if connection is None:
        return None
    client = atlassian_oauth_client(
        session=session, settings=settings, tenant_id=tenant.tenant_id
    )
    access_token = refresh_atlassian_connection_tokens(
        session,
        connection=connection,
        settings=settings,
        tenant_id=tenant.tenant_id,
    )
    source_ref = str(asset.source_ref or "").strip()
    if asset.source_type == "jira_issue":
        issue_key = source_ref.split(":")[-1].strip()
        detail = client.get_issue_detail(
            access_token=access_token,
            cloud_id=connection.cloud_id,
            issue_id_or_key=issue_key,
        )
        return {
            "ok": True,
            "layer": "exact_read",
            "connector": "jira",
            "source_type": asset.source_type,
            "asset_id": asset.asset_id,
            "source_ref": asset.source_ref,
            "title": asset.title,
            "content": detail.description,
            "summary": detail.summary,
            "status": detail.status,
            "metadata": dict(asset.metadata_json or {}),
        }
    if asset.source_type == "jira_comment":
        parts = source_ref.split(":")
        if len(parts) < 3:
            return None
        issue_key = parts[-2].strip()
        comment_id = parts[-1].strip()
        comments = client.list_issue_comments(
            access_token=access_token,
            cloud_id=connection.cloud_id,
            issue_id_or_key=issue_key,
        )
        for comment in comments:
            if str(comment.comment_id) != comment_id:
                continue
            return {
                "ok": True,
                "layer": "exact_read",
                "connector": "jira",
                "source_type": asset.source_type,
                "asset_id": asset.asset_id,
                "source_ref": asset.source_ref,
                "title": asset.title,
                "content": comment.body,
                "metadata": {
                    **dict(asset.metadata_json or {}),
                    "author_display_name": comment.author_display_name,
                    "updated_at": comment.updated_at.isoformat()
                    if comment.updated_at
                    else None,
                },
            }
    return None
