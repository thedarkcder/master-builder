from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.api.admin.schema_mappers import run_to_schema, workflow_to_schema
from orchestrator.api.admin.workflows.live_stream_service import (
    list_workflow_operation_live_events as list_workflow_operation_live_events_impl,
    stream_workflow_operation_live_events_ndjson as stream_workflow_operation_live_events_ndjson_impl,
)
from orchestrator.api.admin.workflows.service import (
    create_workflow_attempt as create_workflow_attempt_impl,
    get_workflow as get_workflow_impl,
    get_workflow_step_audit_attempt as get_workflow_step_audit_attempt_impl,
    get_workflow_step_transcript as get_workflow_step_transcript_impl,
    get_workflow_type_detail as get_workflow_type_detail_impl,
    list_workflow_audit_events as list_workflow_audit_events_impl,
    list_workflow_board_items as list_workflow_board_items_impl,
    list_workflow_telemetry_events as list_workflow_telemetry_events_impl,
    list_workflows as list_workflows_impl,
    list_workflow_types as list_workflow_types_impl,
    resume_workflow_execution as resume_workflow_execution_impl,
    restart_workflow_operation as restart_workflow_operation_impl,
    retry_workflow_operation as retry_workflow_operation_impl,
    preview_start_engineering as preview_start_engineering_impl,
    start_engineering_from_action as start_engineering_from_action_impl,
    start_work_result_to_schema,
)
from orchestrator.api.schemas import (
    RunRead,
    WorkflowExecutionStartRead,
    WorkflowExecutionStartRequest,
    WorkflowBoardItemRead,
    WorkflowObservabilityEventRead,
    WorkflowOperationRestartRequest,
    WorkflowOperationRetryRead,
    WorkflowRead,
    WorkflowStartWorkRead,
    StartEngineeringPreviewRead,
    WorkflowStepAttemptTranscriptRead,
    WorkflowStepTranscriptRead,
    WorkflowTypeDetailRead,
    WorkflowTypeSummaryRead,
)
from orchestrator.core.runtime.agent_runtime_resolver import build_runtime_for_selector
from orchestrator.core.config import get_settings
from orchestrator.core.jira_project_reconciliation.start import start_jira_project_reconciliation
from orchestrator.core.platform.access import PERMISSION_PROJECTS_MANAGE
from orchestrator.core.security import AuthenticatedPrincipal, require_tenant_permission, require_tenant_workspace_access
from orchestrator.core.integrations.workflow.router import WorkflowIntegrationRouter
from orchestrator.core.workflow.type_catalog import get_workflow_type
from orchestrator.runtime.issue_fanout import seed_issues_with_runtime
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import Project, Tenant, WorkflowExecution, WorkflowOperation, WorkflowOperationAttempt

workflow_integration_router = WorkflowIntegrationRouter()


def list_workflows(
    *,
    session: Session,
    principal: AuthenticatedPrincipal,
    tenant_id: str | None,
    project_id: str | None,
    status_filter: str | None,
    issue_query: str | None,
    limit: int,
    offset: int,
) -> list[WorkflowRead]:
    if not principal.is_platform_super_admin:
        if not tenant_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="tenant_id is required for tenant-scoped workflow listing",
            )
        require_tenant_workspace_access(principal=principal, tenant_id=tenant_id)
    return list_workflows_impl(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        status_filter=status_filter,
        issue_query=issue_query,
        limit=limit,
        offset=offset,
        workflow_to_schema_fn=workflow_to_schema,
        run_to_schema_fn=run_to_schema,
    )


def list_workflow_board_items(
    *,
    session: Session,
    principal: AuthenticatedPrincipal,
    tenant_id: str,
    project_id: str | None,
    limit: int,
    offset: int,
) -> list[WorkflowBoardItemRead]:
    require_tenant_workspace_access(principal=principal, tenant_id=tenant_id)
    return list_workflow_board_items_impl(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        limit=limit,
        offset=offset,
    )


def list_workflow_types(
    *,
    session: Session,
    principal: AuthenticatedPrincipal,
    tenant_id: str | None,
) -> list[WorkflowTypeSummaryRead]:
    if not principal.is_platform_super_admin:
        if not tenant_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="tenant_id is required for tenant-scoped workflow type listing",
            )
        require_tenant_workspace_access(principal=principal, tenant_id=tenant_id)
    return list_workflow_types_impl(session=session, tenant_id=tenant_id)


def get_workflow_type_detail(
    *,
    session: Session,
    principal: AuthenticatedPrincipal,
    workflow_type_key: str,
    tenant_id: str | None,
) -> WorkflowTypeDetailRead:
    if not principal.is_platform_super_admin:
        if not tenant_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="tenant_id is required for tenant-scoped workflow type detail",
            )
        require_tenant_workspace_access(principal=principal, tenant_id=tenant_id)
    return get_workflow_type_detail_impl(
        session=session,
        workflow_type_key=workflow_type_key,
        tenant_id=tenant_id,
    )


def get_workflow(*, session: Session, principal: AuthenticatedPrincipal, execution_id: str) -> WorkflowRead:
    _require_workflow_access(session=session, principal=principal, execution_id=execution_id)
    return get_workflow_impl(
        session=session,
        execution_id=execution_id,
        workflow_to_schema_fn=workflow_to_schema,
        run_to_schema_fn=run_to_schema,
    )


def start_workflow_execution(
    *,
    session: Session,
    principal: AuthenticatedPrincipal,
    payload: WorkflowExecutionStartRequest,
) -> WorkflowExecutionStartRead:
    try:
        workflow_type = get_workflow_type(session, workflow_type_key=payload.workflow_type_key)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow type not found") from exc
    if workflow_type.workflow_type_key != "jira_project_reconciliation":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Workflow type does not support direct admin starts",
        )
    return _start_jira_project_reconciliation_workflow(
        session=session,
        principal=principal,
        payload=payload,
    )


def _start_jira_project_reconciliation_workflow(
    *,
    session: Session,
    principal: AuthenticatedPrincipal,
    payload: WorkflowExecutionStartRequest,
) -> WorkflowExecutionStartRead:
    tenant_id = payload.tenant_id.strip()
    if not tenant_id:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="tenant_id is required")
    require_tenant_permission(
        principal=principal,
        tenant_id=tenant_id,
        permission_key=PERMISSION_PROJECTS_MANAGE,
    )
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    project_id = str(payload.project_id or "").strip()
    if not project_id:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="project_id is required")
    project = session.get(Project, project_id)
    if project is None or project.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    if project.is_archived:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Archived projects cannot be reconciled")

    settings = get_settings()
    max_items = _workflow_start_positive_int(
        payload.input.get("max_items"),
        field_name="input.max_items",
        default=max(1, int(getattr(settings, "jira_project_reconciliation_max_items", 1000))),
    )
    try:
        result = start_jira_project_reconciliation(
            session=session,
            settings=settings,
            tenant=tenant,
            project=project,
            max_items=max_items,
            trigger_event="admin_workflow_start",
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return WorkflowExecutionStartRead(
        execution_id=result.execution_id,
        workflow_id=result.workflow_id,
        workflow_type_key=result.workflow_type_key,
        status=result.status,
        started_attempt_id=result.started_attempt_id,
    )


def _workflow_start_positive_int(value: object, *, field_name: str, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"{field_name} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"{field_name} must be an integer") from exc
    if parsed < 1:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{field_name} must be greater than or equal to 1",
        )
    return parsed


def list_workflow_telemetry_events(
    *,
    session: Session,
    principal: AuthenticatedPrincipal,
    execution_id: str,
    limit: int,
    before_recorded_at: datetime | None,
    before_event_id: str | None,
) -> list[WorkflowObservabilityEventRead]:
    _require_workflow_access(session=session, principal=principal, execution_id=execution_id)
    return list_workflow_telemetry_events_impl(
        session=session,
        execution_id=execution_id,
        limit=limit,
        before_recorded_at=before_recorded_at,
        before_event_id=before_event_id,
    )


def list_workflow_audit_events(
    *,
    session: Session,
    principal: AuthenticatedPrincipal,
    execution_id: str,
    limit: int,
    before_recorded_at: datetime | None,
    before_event_id: str | None,
    operation_id: str | None = None,
) -> list[WorkflowObservabilityEventRead]:
    _require_workflow_access(session=session, principal=principal, execution_id=execution_id)
    return list_workflow_audit_events_impl(
        session=session,
        execution_id=execution_id,
        operation_id=operation_id,
        limit=limit,
        before_recorded_at=before_recorded_at,
        before_event_id=before_event_id,
    )


def list_workflow_operation_telemetry_events(
    *,
    session: Session,
    principal: AuthenticatedPrincipal,
    execution_id: str,
    operation_id: str,
    attempt_id: str | None,
    limit: int,
) -> list[WorkflowObservabilityEventRead]:
    workflow = _require_workflow_access(session=session, principal=principal, execution_id=execution_id)
    operation = session.get(WorkflowOperation, operation_id)
    if operation is None or operation.workflow_id != workflow.workflow_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow operation not found")
    return list_workflow_operation_live_events_impl(
        session=session,
        operation=operation,
        attempt_id=attempt_id,
        limit=limit,
    )


def stream_workflow_operation_telemetry_events(
    *,
    principal: AuthenticatedPrincipal,
    execution_id: str,
    operation_id: str,
    attempt_id: str | None,
    after_event_sequence: int | None,
) -> Iterator[str]:
    normalized_attempt_id = str(attempt_id or "").strip() or None
    session_factory = create_session_factory()
    with session_factory() as session:
        workflow = _require_workflow_access(session=session, principal=principal, execution_id=execution_id)
        operation = session.get(WorkflowOperation, operation_id)
        if operation is None or operation.workflow_id != workflow.workflow_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow operation not found")
        normalized_operation_id = operation.operation_id
        if normalized_attempt_id is not None:
            attempt = session.get(WorkflowOperationAttempt, normalized_attempt_id)
            if attempt is None or attempt.operation_id != normalized_operation_id:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow operation attempt not found")
    return stream_workflow_operation_live_events_ndjson_impl(
        operation_id=normalized_operation_id,
        settings=get_settings(),
        attempt_id=normalized_attempt_id,
        after_event_sequence=after_event_sequence,
    )


def get_workflow_operation_transcript(
    *,
    session: Session,
    principal: AuthenticatedPrincipal,
    execution_id: str,
    operation_id: str,
    source: str,
    attempt_id: str | None,
    limit: int,
) -> WorkflowStepTranscriptRead:
    _require_workflow_access(session=session, principal=principal, execution_id=execution_id)
    return get_workflow_step_transcript_impl(
        session=session,
        execution_id=execution_id,
        operation_id=operation_id,
        source=source,
        attempt_id=attempt_id,
        limit=limit,
    )


def get_workflow_operation_attempt_audit(
    *,
    session: Session,
    principal: AuthenticatedPrincipal,
    execution_id: str,
    operation_id: str,
    attempt_id: str,
    limit: int,
) -> WorkflowStepAttemptTranscriptRead:
    _require_workflow_access(session=session, principal=principal, execution_id=execution_id)
    return get_workflow_step_audit_attempt_impl(
        session=session,
        execution_id=execution_id,
        operation_id=operation_id,
        attempt_id=attempt_id,
        limit=limit,
    )


def create_workflow_attempt(
    *,
    session: Session,
    execution_id: str,
    mode: str,
    checkpoint_kind: str | None,
) -> RunRead:
    return create_workflow_attempt_impl(
        session=session,
        execution_id=execution_id,
        mode=mode,
        checkpoint_kind=checkpoint_kind,
        tenant_model=Tenant,
        run_to_schema_fn=run_to_schema,
    )


def resume_workflow_execution(*, session: Session, execution_id: str) -> RunRead:
    return resume_workflow_execution_impl(
        session=session,
        execution_id=execution_id,
        tenant_model=Tenant,
        run_to_schema_fn=run_to_schema,
    )


def retry_workflow_operation(
    *,
    session: Session,
    execution_id: str,
    operation_id: str,
) -> WorkflowOperationRetryRead:
    return retry_workflow_operation_impl(
        session=session,
        execution_id=execution_id,
        operation_id=operation_id,
        workflow_to_schema_fn=workflow_to_schema,
        run_to_schema_fn=run_to_schema,
        integration_router=workflow_integration_router,
        build_runtime_for_selector_fn=build_runtime_for_selector,
        seed_issues_with_runtime_fn=seed_issues_with_runtime,
    )


def restart_workflow_operation(
    *,
    session: Session,
    execution_id: str,
    operation_id: str,
    actor: str,
    payload: WorkflowOperationRestartRequest,
) -> WorkflowOperationRetryRead:
    return restart_workflow_operation_impl(
        session=session,
        execution_id=execution_id,
        operation_id=operation_id,
        actor=actor,
        restart_reason=payload.restart_reason,
        workflow_to_schema_fn=workflow_to_schema,
        run_to_schema_fn=run_to_schema,
        integration_router=workflow_integration_router,
        build_runtime_for_selector_fn=build_runtime_for_selector,
        seed_issues_with_runtime_fn=seed_issues_with_runtime,
    )


def preview_start_engineering(
    *,
    session: Session,
    principal: AuthenticatedPrincipal,
    execution_id: str,
    action_token: str | None,
) -> StartEngineeringPreviewRead:
    return preview_start_engineering_impl(
        session=session,
        principal=principal,
        execution_id=execution_id,
        action_token=action_token,
    )


def start_engineering_from_action(
    *,
    session: Session,
    principal: AuthenticatedPrincipal,
    execution_id: str,
    action_token: str | None,
) -> WorkflowStartWorkRead:
    result, workflow, started_attempt = start_engineering_from_action_impl(
        session=session,
        principal=principal,
        execution_id=execution_id,
        action_token=action_token,
        integration_router=workflow_integration_router,
    )
    return start_work_result_to_schema(
        result=result,
        workflow=workflow,
        started_attempt=started_attempt,
        workflow_to_schema_fn=workflow_to_schema,
        run_to_schema_fn=run_to_schema,
        session=session,
    )


def _require_workflow_access(
    *,
    session: Session,
    principal: AuthenticatedPrincipal,
    execution_id: str,
) -> WorkflowExecution:
    workflow = session.execute(
        select(WorkflowExecution)
        .where(WorkflowExecution.execution_id == execution_id)
        .limit(1)
    ).scalar_one_or_none()
    if workflow is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found")
    if not principal.is_platform_super_admin:
        require_tenant_workspace_access(principal=principal, tenant_id=workflow.tenant_id)
    return workflow
