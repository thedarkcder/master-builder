from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from orchestrator.api.schemas import AdminNotificationListRead, AdminNotificationRead
from orchestrator.core.platform.admin_notifications import list_tenant_admin_notifications
from orchestrator.storage.models import Tenant


def _to_read(row) -> AdminNotificationRead:  # noqa: ANN001
    return AdminNotificationRead(
        notification_id=row.notification_id,
        tenant_id=row.tenant_id,
        project_id=row.project_id,
        scope_type=row.scope_type,
        scope_id=row.scope_id,
        source=row.source,
        kind=row.kind,
        severity=row.severity,
        title=row.title,
        detail=row.detail,
        action_label=row.action_label,
        action_path=row.action_path,
        fingerprint=row.fingerprint,
        status=row.status,
        context=dict(row.context_json or {}),
        first_emitted_at=row.first_emitted_at,
        last_emitted_at=row.last_emitted_at,
        acknowledged_at=row.acknowledged_at,
        resolved_at=row.resolved_at,
    )


def list_tenant_notifications(
    *,
    session: Session,
    tenant_id: str,
    status_filter: str | None = "open",
) -> AdminNotificationListRead:
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    rows = list_tenant_admin_notifications(session=session, tenant_id=tenant_id, status_filter=status_filter)
    return AdminNotificationListRead(notifications=[_to_read(row) for row in rows])
