from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.orm import Session

from orchestrator.storage.models import Project, Tenant

TODO_STATUS = "to do"
DECISION_GATE_COOLDOWN = timedelta(minutes=10)


@dataclass
class JiraWebhookContext:
    request_id: str
    tenant_id: str
    tenant: Tenant
    payload: dict
    webhook_event: str | None
    issue_key: str
    issue_labels: list[str]
    issue_status: str | None
    issue_status_category_key: str | None
    issue_summary: str | None
    issue_description: str | None
    comment_command: str | None
    comment_command_argument: str | None
    comment_command_error: str | None
    delivery_id: str | None
    project: Project | None


@dataclass(frozen=True)
class JiraWebhookContextSnapshot:
    request_id: str
    tenant_id: str
    payload: dict
    webhook_event: str | None
    issue_key: str
    issue_labels: tuple[str, ...]
    issue_status: str | None
    issue_status_category_key: str | None
    issue_summary: str | None
    issue_description: str | None
    comment_command: str | None
    comment_command_argument: str | None
    comment_command_error: str | None
    delivery_id: str | None
    project_id: str | None


def snapshot_jira_webhook_context(*, context: JiraWebhookContext) -> JiraWebhookContextSnapshot:
    return JiraWebhookContextSnapshot(
        request_id=context.request_id,
        tenant_id=context.tenant_id,
        payload=dict(context.payload),
        webhook_event=context.webhook_event,
        issue_key=context.issue_key,
        issue_labels=tuple(context.issue_labels or ()),
        issue_status=context.issue_status,
        issue_status_category_key=context.issue_status_category_key,
        issue_summary=context.issue_summary,
        issue_description=context.issue_description,
        comment_command=context.comment_command,
        comment_command_argument=context.comment_command_argument,
        comment_command_error=context.comment_command_error,
        delivery_id=context.delivery_id,
        project_id=context.project.project_id if context.project is not None else None,
    )


def hydrate_jira_webhook_context(
    *,
    snapshot: JiraWebhookContextSnapshot,
    session: Session,
) -> JiraWebhookContext:
    tenant = session.get(Tenant, snapshot.tenant_id)
    if tenant is None:
        raise RuntimeError(f"Jira webhook tenant '{snapshot.tenant_id}' no longer exists.")
    project = session.get(Project, snapshot.project_id) if snapshot.project_id else None
    return JiraWebhookContext(
        request_id=snapshot.request_id,
        tenant_id=snapshot.tenant_id,
        tenant=tenant,
        payload=dict(snapshot.payload),
        webhook_event=snapshot.webhook_event,
        issue_key=snapshot.issue_key,
        issue_labels=list(snapshot.issue_labels),
        issue_status=snapshot.issue_status,
        issue_status_category_key=snapshot.issue_status_category_key,
        issue_summary=snapshot.issue_summary,
        issue_description=snapshot.issue_description,
        comment_command=snapshot.comment_command,
        comment_command_argument=snapshot.comment_command_argument,
        comment_command_error=snapshot.comment_command_error,
        delivery_id=snapshot.delivery_id,
        project=project,
    )


def jira_webhook_response(
    context: JiraWebhookContext,
    *,
    enqueued: bool,
    reason: str | None,
    **extra: object,
) -> dict:
    payload: dict[str, object] = {
        "request_id": context.request_id,
        "tenant_id": context.tenant_id,
        "project_id": context.project.project_id if context.project is not None else None,
        "issue_key": context.issue_key,
        "enqueued": enqueued,
    }
    if reason is not None:
        payload["reason"] = reason
    payload.update(extra)
    return payload
