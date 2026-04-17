from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from orchestrator.storage.models import AdminNotification, Tenant


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class AdminNotificationInput:
    scope_type: str
    scope_id: str | None
    source: str
    kind: str
    severity: str
    title: str
    detail: str
    fingerprint: str
    tenant_id: str | None = None
    project_id: str | None = None
    action_label: str | None = None
    action_path: str | None = None
    context: dict[str, object] | None = None


def upsert_admin_notification(
    *,
    session: Session,
    notification: AdminNotificationInput,
    now_fn=utcnow,
) -> AdminNotification:
    now = now_fn()
    existing = session.execute(
        select(AdminNotification).where(AdminNotification.fingerprint == notification.fingerprint)
    ).scalar_one_or_none()
    if existing is None:
        row = AdminNotification(
            notification_id=f"notify-{uuid4().hex[:24]}",
            tenant_id=notification.tenant_id,
            project_id=notification.project_id,
            scope_type=notification.scope_type,
            scope_id=notification.scope_id,
            source=notification.source,
            kind=notification.kind,
            severity=notification.severity,
            title=notification.title,
            detail=notification.detail,
            action_label=notification.action_label,
            action_path=notification.action_path,
            fingerprint=notification.fingerprint,
            status="open",
            context_json=dict(notification.context or {}),
            first_emitted_at=now,
            last_emitted_at=now,
            acknowledged_at=None,
            resolved_at=None,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.flush()
        return row

    existing.tenant_id = notification.tenant_id
    existing.project_id = notification.project_id
    existing.scope_type = notification.scope_type
    existing.scope_id = notification.scope_id
    existing.source = notification.source
    existing.kind = notification.kind
    existing.severity = notification.severity
    existing.title = notification.title
    existing.detail = notification.detail
    existing.action_label = notification.action_label
    existing.action_path = notification.action_path
    existing.context_json = dict(notification.context or {})
    existing.status = "open"
    existing.resolved_at = None
    existing.last_emitted_at = now
    existing.updated_at = now
    session.flush()
    return existing


def resolve_admin_notification(
    *,
    session: Session,
    fingerprint: str,
    now_fn=utcnow,
) -> AdminNotification | None:
    row = session.execute(
        select(AdminNotification).where(AdminNotification.fingerprint == fingerprint)
    ).scalar_one_or_none()
    if row is None:
        return None
    now = now_fn()
    row.status = "resolved"
    row.resolved_at = now
    row.updated_at = now
    session.flush()
    return row


def list_tenant_admin_notifications(
    *,
    session: Session,
    tenant_id: str,
    status_filter: str | None = "open",
) -> list[AdminNotification]:
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        return []
    jira_connection_id = str((tenant.jira_config or {}).get("connection_id") or "").strip() or None
    where_clauses = [AdminNotification.tenant_id == tenant_id]
    if jira_connection_id:
        where_clauses.append(
            (
                (AdminNotification.scope_type == "jira_connection")
                & (AdminNotification.scope_id == jira_connection_id)
            )
        )
    query = select(AdminNotification).where(or_(*where_clauses))
    if status_filter:
        query = query.where(AdminNotification.status == status_filter)
    query = query.order_by(AdminNotification.last_emitted_at.desc(), AdminNotification.created_at.desc())
    return list(session.execute(query).scalars().all())
