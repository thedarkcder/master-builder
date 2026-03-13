from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

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
