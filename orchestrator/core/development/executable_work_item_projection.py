from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from orchestrator.core.development.executable_work_items import (
    child_work_item_id,
    parent_work_item_id,
)
from orchestrator.core.jira_project_reconciliation.models import (
    ClassifiedJiraIssue,
    ISSUE_CLASS_ENGINEERING_CHILD,
    ISSUE_CLASS_PARENT,
)
from orchestrator.storage.models import (
    Project,
    Tenant,
    WorkflowExecutableWorkItem,
    WorkflowExecution,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _payload_for(classified_issue: ClassifiedJiraIssue) -> dict[str, object]:
    return {
        "classification": classified_issue.classification,
        "desired_labels": list(classified_issue.desired_labels),
        "labels_changed": classified_issue.labels_changed,
        "issue": classified_issue.issue.to_payload(),
    }


def sync_executable_work_items_for_project(
    *,
    session: Session,
    tenant: Tenant,
    project: Project,
    classified_issues: list[ClassifiedJiraIssue],
    prune_stale: bool,
    seen_at: datetime | None = None,
) -> None:
    """Update the project executable-work projection from a Jira reconciliation result."""
    timestamp = seen_at or _now()
    parent_items = [
        item for item in classified_issues if item.classification == ISSUE_CLASS_PARENT
    ]
    child_items = [
        item
        for item in classified_issues
        if item.classification == ISSUE_CLASS_ENGINEERING_CHILD
    ]

    parent_workflows_by_issue_key = _parent_workflows_by_issue_key(
        session=session,
        tenant_id=tenant.tenant_id,
        project_id=project.project_id,
        issue_keys=[item.issue.key for item in parent_items],
    )
    retained_work_item_ids: set[str] = set()
    for item in parent_items:
        workflow = parent_workflows_by_issue_key.get(item.issue.key)
        if (
            workflow is None
            or str(workflow.status or "").strip().casefold() == "cancelled"
        ):
            continue
        work_item_id = parent_work_item_id(execution_id=workflow.execution_id)
        retained_work_item_ids.add(work_item_id)
        _upsert_work_item(
            session=session,
            work_item_id=work_item_id,
            item_kind="parent",
            tenant_id=tenant.tenant_id,
            project_id=project.project_id,
            parent_workflow_id=workflow.workflow_id,
            parent_execution_id=workflow.execution_id,
            issue_key=item.issue.key,
            parent_issue_key=None,
            issue_summary=item.issue.summary,
            issue_status=item.issue.status,
            issue_type=item.issue.issue_type,
            mb_work_state=item.issue.mb_work_state,
            source_external_id=item.issue.issue_id,
            source_payload=_payload_for(item),
            timestamp=timestamp,
        )

    parent_workflows_by_issue_key.update(
        _parent_workflows_by_issue_key(
            session=session,
            tenant_id=tenant.tenant_id,
            project_id=project.project_id,
            issue_keys=[str(item.issue.parent_key or "") for item in child_items],
        )
    )
    for item in child_items:
        parent_key = str(item.issue.parent_key or "").strip().upper()
        workflow = parent_workflows_by_issue_key.get(parent_key)
        if (
            workflow is None
            or str(workflow.status or "").strip().casefold() == "cancelled"
        ):
            continue
        work_item_id = child_work_item_id(
            execution_id=workflow.execution_id, issue_key=item.issue.key
        )
        retained_work_item_ids.add(work_item_id)
        _upsert_work_item(
            session=session,
            work_item_id=work_item_id,
            item_kind="child",
            tenant_id=tenant.tenant_id,
            project_id=project.project_id,
            parent_workflow_id=workflow.workflow_id,
            parent_execution_id=workflow.execution_id,
            issue_key=item.issue.key,
            parent_issue_key=parent_key,
            issue_summary=item.issue.summary,
            issue_status=item.issue.status,
            issue_type=item.issue.issue_type,
            mb_work_state=item.issue.mb_work_state,
            source_external_id=item.issue.issue_id,
            source_payload=_payload_for(item),
            timestamp=timestamp,
        )

    if prune_stale:
        stale_query = delete(WorkflowExecutableWorkItem).where(
            WorkflowExecutableWorkItem.tenant_id == tenant.tenant_id,
            WorkflowExecutableWorkItem.project_id == project.project_id,
        )
        if retained_work_item_ids:
            stale_query = stale_query.where(
                WorkflowExecutableWorkItem.work_item_id.not_in(retained_work_item_ids)
            )
        session.execute(stale_query)


def _parent_workflows_by_issue_key(
    *,
    session: Session,
    tenant_id: str,
    project_id: str,
    issue_keys: list[str],
) -> dict[str, WorkflowExecution]:
    normalized_issue_keys = sorted(
        {
            str(issue_key or "").strip().upper()
            for issue_key in issue_keys
            if str(issue_key or "").strip()
        }
    )
    if not normalized_issue_keys:
        return {}
    rows = session.execute(
        select(WorkflowExecution).where(
            WorkflowExecution.tenant_id == tenant_id,
            WorkflowExecution.project_id == project_id,
            WorkflowExecution.workflow_type_key == "parent_planning",
            WorkflowExecution.source_system == "jira",
            WorkflowExecution.source_ref.in_(normalized_issue_keys),
        )
    ).scalars()
    return {str(row.source_ref or "").strip().upper(): row for row in rows}


def _upsert_work_item(
    *,
    session: Session,
    work_item_id: str,
    item_kind: str,
    tenant_id: str,
    project_id: str,
    parent_workflow_id: str,
    parent_execution_id: str,
    issue_key: str,
    parent_issue_key: str | None,
    issue_summary: str | None,
    issue_status: str | None,
    issue_type: str | None,
    mb_work_state: str | None,
    source_external_id: str | None,
    source_payload: dict[str, object],
    timestamp: datetime,
) -> None:
    existing = session.get(WorkflowExecutableWorkItem, work_item_id)
    if existing is None:
        existing = WorkflowExecutableWorkItem(
            work_item_id=work_item_id,
            item_kind=item_kind,
            tenant_id=tenant_id,
            project_id=project_id,
            parent_workflow_id=parent_workflow_id,
            parent_execution_id=parent_execution_id,
            issue_key=issue_key,
            parent_issue_key=parent_issue_key,
            issue_summary=issue_summary,
            issue_status=issue_status,
            issue_type=issue_type,
            mb_work_state=mb_work_state,
            source_system="jira",
            source_external_id=source_external_id,
            source_payload_json=source_payload,
            created_at=timestamp,
            updated_at=timestamp,
            last_seen_at=timestamp,
        )
        session.add(existing)
        return

    existing.item_kind = item_kind
    existing.tenant_id = tenant_id
    existing.project_id = project_id
    existing.parent_workflow_id = parent_workflow_id
    existing.parent_execution_id = parent_execution_id
    existing.issue_key = issue_key
    existing.parent_issue_key = parent_issue_key
    existing.issue_summary = issue_summary
    existing.issue_status = issue_status
    existing.issue_type = issue_type
    existing.mb_work_state = mb_work_state
    existing.source_system = "jira"
    existing.source_external_id = source_external_id
    existing.source_payload_json = source_payload
    existing.updated_at = timestamp
    existing.last_seen_at = timestamp
