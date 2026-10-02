from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, status
from sqlalchemy import select

from orchestrator.api.admin.schema_mappers import workflow_operation_attempt_to_schema
from orchestrator.api.admin.workflows.execution_read_service import workflow_schema
from orchestrator.api.admin.workflows.queries import workflow_by_execution_id
from orchestrator.api.schemas import (
    StartEngineeringPreviewRead,
    StartWorkIssueRead,
    WorkflowStartWorkRead,
)
from orchestrator.api.webhooks.contracts import create_jira_comment, post_jira_comment
from orchestrator.core.config import get_settings
from orchestrator.core.development.start_work import StartWorkUseCase
from orchestrator.core.development.start_work_links import (
    verify_start_work_action_token,
)
from orchestrator.core.integrations.atlassian.parent_child_sync_shared import (
    JiraParentChildSyncContext,
)
from orchestrator.core.parent_feature_workflow.adapters import _JiraParentIssueGateway
from orchestrator.core.security import (
    AuthenticatedPrincipal,
    require_tenant_workspace_access,
)
from orchestrator.storage.models import (
    Project,
    Tenant,
    WorkflowExecutableWorkItem,
    WorkflowExecution,
    WorkflowOperationAttempt,
)


@dataclass(frozen=True)
class StartWorkItemBoardResult:
    work_item_id: str
    action: str
    result: object
    workflow: object
    started_attempt: object | None

    @property
    def queued(self):  # noqa: ANN201
        return self.result.queued

    @property
    def skipped(self):  # noqa: ANN201
        return self.result.skipped


def start_work_result_to_schema(
    *,
    result,
    workflow,
    started_attempt,
    workflow_to_schema_fn,
    run_to_schema_fn,
    session,
):  # noqa: ANN001
    return WorkflowStartWorkRead(
        workflow=workflow_schema(
            session=session,
            workflow=workflow,
            workflow_to_schema_fn=workflow_to_schema_fn,
            run_to_schema_fn=run_to_schema_fn,
        ),
        queued=[
            StartWorkIssueRead(
                issue_key=item.issue_key,
                run_id=item.run_id,
                status=item.status,
                reason=item.reason,
            )
            for item in result.queued
        ],
        skipped=[
            StartWorkIssueRead(
                issue_key=item.issue_key,
                run_id=item.run_id,
                status=item.status,
                reason=item.reason,
            )
            for item in result.skipped
        ],
        promoted_issue_keys=list(result.promoted_issue_keys),
        started_attempt=workflow_operation_attempt_to_schema(started_attempt)
        if started_attempt is not None
        else None,
    )


def _workflow_for_start_engineering(*, session, execution_id: str):  # noqa: ANN001
    workflow = workflow_by_execution_id(session=session, execution_id=execution_id)
    if workflow is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found"
        )
    if str(workflow.source_system or "").strip() != "jira":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Development start requires a Jira workflow",
        )
    if str(workflow.workflow_type_key or "").strip() != "parent_planning":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Development start requires a parent planning workflow",
        )
    return workflow


def _validate_start_engineering_access(
    *,
    session,
    workflow,
    principal: AuthenticatedPrincipal,
    action_token: str | None,
):  # noqa: ANN001
    normalized_action_token = str(action_token or "").strip()
    if not normalized_action_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Start engineering action token is required",
        )
    settings = get_settings()
    try:
        claims = verify_start_work_action_token(
            token=normalized_action_token,
            secret=settings.jira_action_token_secret,
            expected_execution_id=workflow.execution_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)
        ) from exc
    if (
        claims.tenant_id != workflow.tenant_id
        or claims.workflow_id != workflow.workflow_id
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Start engineering action token does not match this workflow",
        )
    project_id = str(workflow.project_id or "").strip()
    if claims.project_id != project_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Start engineering action token does not match this project",
        )
    if claims.issue_key != str(workflow.source_ref or "").strip().upper():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Start engineering action token does not match this issue",
        )
    require_tenant_workspace_access(principal=principal, tenant_id=claims.tenant_id)
    tenant = session.get(Tenant, claims.tenant_id)
    project = session.get(Project, claims.project_id)
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Workflow tenant is missing"
        )
    if project is None or project.tenant_id != tenant.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Workflow project is not available",
        )
    return claims, tenant, project, settings


def preview_start_engineering(
    *,
    session,
    execution_id: str,
    principal: AuthenticatedPrincipal,
    action_token: str | None,
) -> StartEngineeringPreviewRead:  # noqa: ANN001
    workflow = _workflow_for_start_engineering(
        session=session, execution_id=execution_id
    )
    claims, _tenant, _project, _settings = _validate_start_engineering_access(
        session=session,
        workflow=workflow,
        principal=principal,
        action_token=action_token,
    )
    workflow_status = str(workflow.status or "").strip().lower()
    can_start = workflow_status == "completed"
    return StartEngineeringPreviewRead(
        tenant_id=claims.tenant_id,
        project_id=claims.project_id,
        execution_id=workflow.execution_id,
        issue_key=claims.issue_key,
        display_name=workflow.display_name,
        workflow_status=workflow_status,
        can_start=can_start,
        unavailable_reason=None
        if can_start
        else "Parent planning must be completed before engineering can start",
    )


def start_engineering_from_action(
    *,
    session,
    execution_id: str,
    principal: AuthenticatedPrincipal,
    action_token: str | None,
    integration_router,
):  # noqa: ANN001
    workflow = _workflow_for_start_engineering(
        session=session, execution_id=execution_id
    )
    if str(workflow.status or "").strip().lower() != "completed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Parent planning must be completed before development can start",
        )
    claims, tenant, project, settings = _validate_start_engineering_access(
        session=session,
        workflow=workflow,
        principal=principal,
        action_token=action_token,
    )
    gateway = _JiraParentIssueGateway(
        session=session,
        settings=settings,
        context=JiraParentChildSyncContext(
            request_id=f"start-engineering:{workflow.execution_id}",
            tenant_id=tenant.tenant_id,
            tenant=tenant,
            project_id=project.project_id,
            issue_key=claims.issue_key,
            issue_labels=[],
            payload={},
            webhook_event="start_engineering_action",
            comment_command=None,
            comment_command_argument=None,
        ),
        integration_router=integration_router,
        post_jira_comment_fn=post_jira_comment,
        create_jira_comment_fn=create_jira_comment,
    )
    actor = (
        principal.email or principal.username or principal.user_id or "jira_action_link"
    )
    try:
        result = StartWorkUseCase(session=session, issue_gateway=gateway).start(
            tenant=tenant,
            project=project,
            issue_key=claims.issue_key,
            target_status="To Do",
            actor=actor,
            reason="jira_start_engineering_link",
            source_workflow_id=workflow.workflow_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    return (
        result,
        workflow,
        session.get(WorkflowOperationAttempt, result.attempt_id)
        if result.attempt_id
        else None,
    )


def start_work_item_from_board(
    *,
    session,
    work_item_id: str,
    principal: AuthenticatedPrincipal,
    integration_router,
):  # noqa: ANN001
    work_item = session.get(WorkflowExecutableWorkItem, str(work_item_id or "").strip())
    if work_item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Executable work item not found",
        )
    workflow = session.get(WorkflowExecution, work_item.parent_workflow_id)
    if workflow is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Executable work item parent workflow is missing",
        )
    if work_item.item_kind == "parent":
        if str(workflow.status or "").strip().casefold() != "completed":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Parent planning must complete before parent work can start",
            )
        existing_children = session.execute(
            select(WorkflowExecutableWorkItem.work_item_id)
            .where(
                WorkflowExecutableWorkItem.parent_workflow_id == workflow.workflow_id,
                WorkflowExecutableWorkItem.item_kind == "child",
            )
            .limit(1)
        ).scalar_one_or_none()
        if existing_children is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Start child work items individually",
            )
    elif work_item.item_kind != "child":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Unsupported executable work item kind",
        )
    if str(work_item.issue_status or "").strip().casefold() in {
        "done",
        "closed",
        "released",
        "release ready",
        "ready to release",
    }:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Executable work item is not ready to start",
        )
    result, workflow, started_attempt = _start_projected_issue_from_board(
        session=session,
        workflow=workflow,
        work_item=work_item,
        principal=principal,
        integration_router=integration_router,
    )
    return StartWorkItemBoardResult(
        work_item_id=work_item.work_item_id,
        action="engineering",
        result=result,
        workflow=workflow,
        started_attempt=started_attempt,
    )


def _start_projected_issue_from_board(
    *,
    session,
    workflow: WorkflowExecution,
    work_item: WorkflowExecutableWorkItem,
    principal: AuthenticatedPrincipal,
    integration_router,
):  # noqa: ANN001
    require_tenant_workspace_access(principal=principal, tenant_id=workflow.tenant_id)
    tenant = session.get(Tenant, workflow.tenant_id)
    project_id = str(workflow.project_id or "").strip()
    project = session.get(Project, project_id) if project_id else None
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Workflow tenant is missing"
        )
    if project is None or project.tenant_id != tenant.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Workflow project is not available",
        )
    parent_issue_key = str(workflow.source_ref or "").strip().upper()
    issue_key = str(work_item.issue_key or "").strip().upper()
    if not issue_key:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Executable work item is missing issue key",
        )
    settings = get_settings()
    gateway = _JiraParentIssueGateway(
        session=session,
        settings=settings,
        context=JiraParentChildSyncContext(
            request_id=f"start-work-item-board:{work_item.work_item_id}",
            tenant_id=tenant.tenant_id,
            tenant=tenant,
            project_id=project.project_id,
            issue_key=parent_issue_key,
            issue_labels=[],
            payload={"work_item_id": work_item.work_item_id},
            webhook_event="start_work_item_board",
            comment_command=None,
            comment_command_argument=None,
        ),
        integration_router=integration_router,
        post_jira_comment_fn=post_jira_comment,
        create_jira_comment_fn=create_jira_comment,
    )
    actor = (
        principal.email
        or principal.username
        or principal.user_id
        or "board_start_work_item"
    )
    try:
        result = StartWorkUseCase(session=session, issue_gateway=gateway).start(
            tenant=tenant,
            project=project,
            issue_key=issue_key,
            target_status="To Do",
            actor=actor,
            reason="project_board_start_work_item",
            require_source_workflow_completed=False,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    return (
        result,
        workflow,
        session.get(WorkflowOperationAttempt, result.attempt_id)
        if result.attempt_id
        else None,
    )
