from __future__ import annotations

from fastapi import HTTPException, status

from orchestrator.api.admin.schema_mappers import run_to_schema
from orchestrator.api.admin.workflows.execution_read_service import workflow_schema
from orchestrator.api.admin.workflows.queries import workflow_by_execution_id
from orchestrator.api.webhooks.contracts import (
    create_jira_comment,
    extract_changed_fields,
    extract_status_transition,
    post_jira_comment,
)
from orchestrator.core.config import get_settings
from orchestrator.core.integrations.atlassian.parent_child_sync_shared import (
    JiraParentChildSyncContext,
)
from orchestrator.core.parent_feature_workflow.flows import (
    handle_parent_feature_sync as handle_parent_feature_sync_service,
)
from orchestrator.core.runtime.agent_runtime_resolver import build_runtime_for_selector
from orchestrator.core.workflow.runtime import build_workflow_runtime
from orchestrator.runtime.issue_fanout import seed_issues_with_runtime
from orchestrator.storage.models import Project, Tenant


def start_parent_planning(
    *,
    session,
    execution_id: str,
    integration_router,
    workflow_to_schema_fn,
):  # noqa: ANN001
    workflow = workflow_by_execution_id(session=session, execution_id=execution_id)
    if workflow is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found"
        )
    if str(workflow.workflow_type_key or "").strip() != "parent_planning":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only parent planning workflows can be started",
        )
    workflow_status = str(workflow.status or "").strip().lower()
    if workflow_status not in {"queued", "pending"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Parent planning cannot be started from status {workflow.status}",
        )
    if str(workflow.source_system or "").strip() != "jira":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Parent planning start requires a Jira source",
        )

    tenant = session.get(Tenant, workflow.tenant_id)
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Workflow tenant not found"
        )
    project_id = str(workflow.project_id or "").strip()
    if not project_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Workflow project is required"
        )
    project = session.get(Project, project_id)
    if project is None or project.tenant_id != tenant.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Workflow project not found"
        )
    if project.is_archived:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Archived projects cannot start planning",
        )

    settings = get_settings()
    issue_key = str(workflow.source_ref or "").strip().upper()
    jira = integration_router.jira(session=session, tenant=tenant, settings=settings)
    try:
        issue_detail = jira.get_issue_detail(issue_id_or_key=issue_key)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to load Jira issue {issue_key}: {exc}",
        ) from exc

    labels = [
        str(label).strip()
        for label in (getattr(issue_detail, "labels", None) or [])
        if str(label).strip()
    ]
    if "pm-parent" not in {label.casefold() for label in labels}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Jira issue {issue_key} is not labelled pm-parent",
        )

    result = handle_parent_feature_sync_service(
        context=JiraParentChildSyncContext(
            request_id=f"start-parent-planning:{workflow.execution_id}",
            tenant_id=tenant.tenant_id,
            tenant=tenant,
            project_id=project.project_id,
            issue_key=issue_key,
            issue_labels=labels,
            payload={"request_id": f"start-parent-planning:{workflow.execution_id}"},
            webhook_event="issue_created",
            comment_command=None,
            comment_command_argument=None,
        ),
        session=session,
        settings=settings,
        integration_router=integration_router,
        extract_changed_fields_fn=extract_changed_fields,
        extract_status_transition_fn=extract_status_transition,
        build_workflow_runtime_fn=build_workflow_runtime,
        build_runtime_for_selector_fn=build_runtime_for_selector,
        seed_issues_with_runtime_fn=seed_issues_with_runtime,
        post_jira_comment_fn=post_jira_comment,
        create_jira_comment_fn=create_jira_comment,
    )
    if result.failed:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(
                result.extra.get("error")
                or result.reason
                or "Parent planning start failed"
            ),
        )
    if not result.handled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=result.reason or "Parent planning start was not handled",
        )

    session.expire_all()
    refreshed = workflow_by_execution_id(session=session, execution_id=execution_id)
    if refreshed is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workflow not found after planning start",
        )
    return workflow_schema(
        session=session,
        workflow=refreshed,
        workflow_to_schema_fn=workflow_to_schema_fn,
        run_to_schema_fn=run_to_schema,
    )
