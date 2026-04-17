from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from orchestrator.storage.models import AdminNotification, Tenant


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class AdminNotificationScope:
    scope_type: str
    scope_id: str | None = None
    tenant_id: str | None = None
    project_id: str | None = None


@dataclass(frozen=True)
class AdminNotificationTemplate:
    severity: str
    title: str
    action_label: str | None = None
    action_path: str | None = None


ADMIN_NOTIFICATION_KIND_JIRA_CONNECTION_REAUTH_REQUIRED = "reauth_required"

_NOTIFICATION_TEMPLATES: dict[str, AdminNotificationTemplate] = {
    ADMIN_NOTIFICATION_KIND_JIRA_CONNECTION_REAUTH_REQUIRED: AdminNotificationTemplate(
        severity="HIGH",
        title="Jira connection needs reauthentication",
        action_label="Reconnect Jira",
        action_path=None,
    ),
}


@dataclass(frozen=True)
class AdminNotificationDraft:
    scope: AdminNotificationScope
    source: str
    kind: str
    detail: str
    dedupe_key: str | None = None
    severity: str | None = None
    title: str | None = None
    tenant_id: str | None = None
    project_id: str | None = None
    action_label: str | None = None
    action_path: str | None = None
    context: dict[str, object] = field(default_factory=dict)


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


def notification_fingerprint_for(
    *,
    scope: AdminNotificationScope,
    kind: str,
    dedupe_key: str | None = None,
) -> str:
    payload = {
        "scope_type": str(scope.scope_type or "").strip(),
        "scope_id": str(scope.scope_id or "").strip() or None,
        "tenant_id": str(scope.tenant_id or "").strip() or None,
        "project_id": str(scope.project_id or "").strip() or None,
        "kind": str(kind or "").strip(),
        "dedupe_key": str(dedupe_key or "").strip() or None,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _template_for(kind: str) -> AdminNotificationTemplate | None:
    return _NOTIFICATION_TEMPLATES.get(str(kind or "").strip())


def emit_admin_notification(
    *,
    session: Session,
    notification: AdminNotificationDraft,
    now_fn=utcnow,
) -> AdminNotification:
    template = _template_for(notification.kind)
    scope = notification.scope
    severity = str(notification.severity or (template.severity if template else "")).strip()
    title = str(notification.title or (template.title if template else "")).strip()
    if not severity or not title:
        raise ValueError("notification severity and title are required")
    return upsert_admin_notification(
        session=session,
        notification=AdminNotificationInput(
            tenant_id=notification.tenant_id if notification.tenant_id is not None else scope.tenant_id,
            project_id=notification.project_id if notification.project_id is not None else scope.project_id,
            scope_type=str(scope.scope_type or "").strip(),
            scope_id=str(scope.scope_id or "").strip() or None,
            source=str(notification.source or "").strip(),
            kind=str(notification.kind or "").strip(),
            severity=severity,
            title=title,
            detail=str(notification.detail or "").strip(),
            fingerprint=notification_fingerprint_for(
                scope=scope,
                kind=notification.kind,
                dedupe_key=notification.dedupe_key,
            ),
            action_label=notification.action_label if notification.action_label is not None else (template.action_label if template else None),
            action_path=notification.action_path if notification.action_path is not None else (template.action_path if template else None),
            context=dict(notification.context or {}),
        ),
        now_fn=now_fn,
    )


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


def resolve_admin_notification_state(
    *,
    session: Session,
    scope: AdminNotificationScope,
    kind: str,
    dedupe_key: str | None = None,
    now_fn=utcnow,
) -> AdminNotification | None:
    return resolve_admin_notification(
        session=session,
        fingerprint=notification_fingerprint_for(scope=scope, kind=kind, dedupe_key=dedupe_key),
        now_fn=now_fn,
    )


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
