from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session
from temporalio.client import WorkflowUpdateFailedError

from orchestrator.api.admin.schema_mappers import (
    run_to_schema,
    workflow_operation_attempt_to_schema,
    workflow_to_schema,
)
from orchestrator.api.admin.workflows.execution_read_service import workflow_schema
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
    start_parent_planning as start_parent_planning_impl,
    start_work_item_from_board as start_work_item_from_board_impl,
    start_work_result_to_schema,
)
from orchestrator.api.schemas import (
    StartWorkIssueRead,
    RunRead,
    WorkflowExecutionStartRead,
    WorkflowExecutionStartRequest,
    WorkflowBoardItemRead,
    WorkflowObservabilityEventRead,
    WorkflowOperationRestartRequest,
    WorkflowOperationRetryRead,
    WorkflowRead,
    WorkflowStartWorkRead,
    WorkflowWorkItemStartRead,
    StartEngineeringPreviewRead,
    WorkflowStepAttemptTranscriptRead,
    WorkflowStepTranscriptRead,
    WorkflowTypeDetailRead,
    WorkflowTypeSummaryRead,
)
from orchestrator.core.runtime.agent_runtime_resolver import build_runtime_for_selector
from orchestrator.core.config import get_settings
from orchestrator.core.development.executable_work_items import parse_work_item_id
from orchestrator.core.jira_project_reconciliation.start import (
    start_jira_project_reconciliation,
)
from orchestrator.core.platform.admin_notifications import (
    ADMIN_NOTIFICATION_KIND_JIRA_CONNECTION_REAUTH_REQUIRED,
    AdminNotificationDraft,
    AdminNotificationScope,
    emit_admin_notification,
)
from orchestrator.core.platform.access import PERMISSION_PROJECTS_MANAGE
from orchestrator.core.qa.demo_proof_start import start_demo_proof_workflow
from orchestrator.core.security import (
    AuthenticatedPrincipal,
    require_tenant_permission,
    require_tenant_workspace_access,
)
from orchestrator.core.integrations.workflow.router import WorkflowIntegrationRouter
from orchestrator.core.workflow.type_catalog import get_workflow_type
from orchestrator.runtime.issue_fanout import seed_issues_with_runtime
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import (
    AtlassianOAuthConnection,
    Project,
    Tenant,
    WorkflowExecutableWorkItem,
    WorkflowExecution,
    WorkflowOperation,
    WorkflowOperationAttempt,
)

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


def get_workflow(
    *, session: Session, principal: AuthenticatedPrincipal, execution_id: str
) -> WorkflowRead:
    _require_workflow_access(
        session=session, principal=principal, execution_id=execution_id
    )
    return get_workflow_impl(
        session=session,
        execution_id=execution_id,
        workflow_to_schema_fn=workflow_to_schema,
        run_to_schema_fn=run_to_schema,
    )


def start_parent_planning(
    *, session: Session, principal: AuthenticatedPrincipal, execution_id: str
) -> WorkflowRead:
    workflow = _require_workflow_access(
        session=session, principal=principal, execution_id=execution_id
    )
    require_tenant_permission(
        principal=principal,
        tenant_id=workflow.tenant_id,
        permission_key=PERMISSION_PROJECTS_MANAGE,
    )
    return start_parent_planning_impl(
        session=session,
        execution_id=execution_id,
        integration_router=workflow_integration_router,
        workflow_to_schema_fn=workflow_to_schema,
    )


def start_work_item_from_board(
    *,
    session: Session,
    principal: AuthenticatedPrincipal,
    work_item_id: str,
) -> WorkflowWorkItemStartRead:
    try:
        ref = parse_work_item_id(work_item_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    work_item = session.get(WorkflowExecutableWorkItem, work_item_id)
    if work_item is None or work_item.item_kind != ref.kind:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Executable work item not found",
        )
    workflow = _require_workflow_access(
        session=session, principal=principal, execution_id=ref.execution_id
    )
    if work_item.parent_workflow_id != workflow.workflow_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Executable work item does not match workflow",
        )
    require_tenant_permission(
        principal=principal,
        tenant_id=workflow.tenant_id,
        permission_key=PERMISSION_PROJECTS_MANAGE,
    )
    if ref.kind == "parent":
        workflow_status = str(workflow.status or "").strip().casefold()
        if workflow_status in {"queued", "pending"}:
            refreshed_workflow = start_parent_planning_impl(
                session=session,
                execution_id=ref.execution_id,
                integration_router=workflow_integration_router,
                workflow_to_schema_fn=workflow_to_schema,
            )
            return WorkflowWorkItemStartRead(
                work_item_id=work_item_id,
                action="planning",
                workflow=refreshed_workflow,
            )
        if workflow_status != "completed":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Parent work item cannot be started from status {workflow.status}",
            )
        board_result = start_work_item_from_board_impl(
            session=session,
            work_item_id=work_item_id,
            principal=principal,
            integration_router=workflow_integration_router,
        )
        return WorkflowWorkItemStartRead(
            work_item_id=work_item_id,
            action="engineering",
            workflow=workflow_schema(
                session=session,
                workflow=board_result.workflow,
                workflow_to_schema_fn=workflow_to_schema,
                run_to_schema_fn=run_to_schema,
            ),
            queued=[
                StartWorkIssueRead(
                    issue_key=item.issue_key,
                    run_id=item.run_id,
                    status=item.status,
                    reason=item.reason,
                )
                for item in board_result.queued
            ],
            skipped=[
                StartWorkIssueRead(
                    issue_key=item.issue_key,
                    run_id=item.run_id,
                    status=item.status,
                    reason=item.reason,
                )
                for item in board_result.skipped
            ],
            promoted_issue_keys=list(board_result.result.promoted_issue_keys),
            started_attempt=workflow_operation_attempt_to_schema(
                board_result.started_attempt
            )
            if board_result.started_attempt is not None
            else None,
        )
    board_result = start_work_item_from_board_impl(
        session=session,
        work_item_id=work_item_id,
        principal=principal,
        integration_router=workflow_integration_router,
    )
    return WorkflowWorkItemStartRead(
        work_item_id=board_result.work_item_id,
        action=board_result.action,
        workflow=workflow_schema(
            session=session,
            workflow=board_result.workflow,
            workflow_to_schema_fn=workflow_to_schema,
            run_to_schema_fn=run_to_schema,
        ),
        queued=[
            StartWorkIssueRead(
                issue_key=item.issue_key,
                run_id=item.run_id,
                status=item.status,
                reason=item.reason,
            )
            for item in board_result.queued
        ],
        skipped=[
            StartWorkIssueRead(
                issue_key=item.issue_key,
                run_id=item.run_id,
                status=item.status,
                reason=item.reason,
            )
            for item in board_result.skipped
        ],
        promoted_issue_keys=list(board_result.result.promoted_issue_keys),
        started_attempt=workflow_operation_attempt_to_schema(
            board_result.started_attempt
        )
        if board_result.started_attempt is not None
        else None,
    )


def start_workflow_execution(
    *,
    session: Session,
    principal: AuthenticatedPrincipal,
    payload: WorkflowExecutionStartRequest,
) -> WorkflowExecutionStartRead:
    try:
        workflow_type = get_workflow_type(
            session, workflow_type_key=payload.workflow_type_key
        )
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Workflow type not found"
        ) from exc
    if workflow_type.workflow_type_key == "jira_project_reconciliation":
        return _start_jira_project_reconciliation_workflow(
            session=session,
            principal=principal,
            payload=payload,
        )
    if workflow_type.workflow_type_key == "demo_proof":
        return _start_demo_proof_workflow(
            session=session,
            principal=principal,
            payload=payload,
        )
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="Workflow type does not support direct admin starts",
    )


def _require_start_tenant_and_project(
    *,
    session: Session,
    principal: AuthenticatedPrincipal,
    payload: WorkflowExecutionStartRequest,
) -> tuple[Tenant, Project]:
    tenant_id = payload.tenant_id.strip()
    if not tenant_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="tenant_id is required",
        )
    require_tenant_permission(
        principal=principal,
        tenant_id=tenant_id,
        permission_key=PERMISSION_PROJECTS_MANAGE,
    )
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found"
        )
    project_id = str(payload.project_id or "").strip()
    if not project_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="project_id is required",
        )
    project = session.get(Project, project_id)
    if project is None or project.tenant_id != tenant_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        )
    return tenant, project


def _start_demo_proof_workflow(
    *,
    session: Session,
    principal: AuthenticatedPrincipal,
    payload: WorkflowExecutionStartRequest,
) -> WorkflowExecutionStartRead:
    tenant, project = _require_start_tenant_and_project(
        session=session,
        principal=principal,
        payload=payload,
    )
    if project.is_archived:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Archived projects cannot run demo proof",
        )
    settings = get_settings()
    proof_scope_id = str(payload.input.get("proof_scope_id") or "").strip()
    commit_sha = str(payload.input.get("commit_sha") or "").strip()
    trigger_mode = str(payload.input.get("trigger_mode") or "").strip()
    run_id = str(payload.input.get("run_id") or "").strip() or None
    release_id = str(payload.input.get("release_id") or "").strip() or None
    pr_url = str(payload.input.get("pr_url") or "").strip() or None
    if trigger_mode == "from_pr" and run_id is not None:
        raise HTTPException(
            status_code=422,
            detail="Demo proof from_pr must not include run_id",
        )
    if trigger_mode == "from_pr" and release_id is not None:
        raise HTTPException(
            status_code=422,
            detail="Demo proof from_pr must not include release_id",
        )
    required_capture_targets = payload.input.get("required_capture_targets")
    if required_capture_targets is not None and not isinstance(
        required_capture_targets, list
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="input.required_capture_targets must be a list",
        )
    required_recording_counts = payload.input.get("required_recording_counts")
    if required_recording_counts is not None and not isinstance(
        required_recording_counts, dict
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="input.required_recording_counts must be an object",
        )
    try:
        result = start_demo_proof_workflow(
            session=session,
            settings=settings,
            tenant=tenant,
            project=project,
            proof_scope_id=proof_scope_id,
            commit_sha=commit_sha,
            trigger_mode=trigger_mode,
            run_id=run_id,
            release_id=release_id,
            pr_url=pr_url,
            required_capture_targets=required_capture_targets,
            required_recording_counts=required_recording_counts,
            trigger_event="admin_workflow_start",
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except WorkflowUpdateFailedError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Demo proof workflow could not start because the workflow update failed.",
        ) from exc
    return WorkflowExecutionStartRead(
        execution_id=result.execution_id,
        workflow_id=result.workflow_id,
        workflow_type_key=result.workflow_type_key,
        status=result.status,
        started_attempt_id=result.started_attempt_id,
    )


def _start_jira_project_reconciliation_workflow(
    *,
    session: Session,
    principal: AuthenticatedPrincipal,
    payload: WorkflowExecutionStartRequest,
) -> WorkflowExecutionStartRead:
    tenant, project = _require_start_tenant_and_project(
        session=session,
        principal=principal,
        payload=payload,
    )
    if project.is_archived:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Archived projects cannot be reconciled",
        )

    settings = get_settings()
    max_items = _workflow_start_positive_int(
        payload.input.get("max_items"),
        field_name="input.max_items",
        default=max(
            1, int(getattr(settings, "jira_project_reconciliation_max_items", 1000))
        ),
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
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    except WorkflowUpdateFailedError as exc:
        if _workflow_update_failure_requires_jira_reauth(exc):
            _emit_jira_reauth_required_notification(session=session, tenant=tenant)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Atlassian connection requires reauthentication.",
            ) from exc
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Jira sync could not start because the workflow update failed.",
        ) from exc
    return WorkflowExecutionStartRead(
        execution_id=result.execution_id,
        workflow_id=result.workflow_id,
        workflow_type_key=result.workflow_type_key,
        status=result.status,
        started_attempt_id=result.started_attempt_id,
    )


def _workflow_update_failure_requires_jira_reauth(exc: BaseException) -> bool:
    messages: list[str] = []
    current: BaseException | None = exc
    for _ in range(8):
        if current is None:
            break
        messages.append(str(current))
        if isinstance(current, WorkflowUpdateFailedError):
            current = current.cause
            continue
        current = current.__cause__ or current.__context__
    normalized = "\n".join(messages).lower()
    return (
        "refresh_token is invalid" in normalized
        or "unauthorized_client" in normalized
        or "atlassian connection requires reauthentication" in normalized
        or "atlassian request failed (401)" in normalized
        or "atlassian request failed (403)" in normalized
    )


def _emit_jira_reauth_required_notification(
    *, session: Session, tenant: Tenant
) -> None:
    connection_id = str((tenant.jira_config or {}).get("connection_id") or "").strip()
    tenant_id = tenant.tenant_id
    session.rollback()
    if not connection_id:
        return

    connection = session.get(AtlassianOAuthConnection, connection_id)
    emit_admin_notification(
        session=session,
        notification=AdminNotificationDraft(
            scope=AdminNotificationScope(
                scope_type="jira_connection",
                scope_id=connection_id,
                tenant_id=tenant_id,
            ),
            source="atlassian_oauth",
            kind=ADMIN_NOTIFICATION_KIND_JIRA_CONNECTION_REAUTH_REQUIRED,
            detail=(
                "Stored Atlassian credentials are no longer valid. Reconnect Atlassian from tenant settings to "
                "restore project loading, issue sync, and webhook administration."
            ),
            dedupe_key="reauth_required",
            context={
                "connection_id": connection_id,
                "site_url": connection.site_url if connection is not None else None,
                "failure_category": "invalid_refresh_token",
            },
        ),
    )
    session.commit()


def _workflow_start_positive_int(
    value: object, *, field_name: str, default: int
) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{field_name} must be an integer",
        )
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{field_name} must be an integer",
        ) from exc
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
    _require_workflow_access(
        session=session, principal=principal, execution_id=execution_id
    )
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
    _require_workflow_access(
        session=session, principal=principal, execution_id=execution_id
    )
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
    workflow = _require_workflow_access(
        session=session, principal=principal, execution_id=execution_id
    )
    operation = session.get(WorkflowOperation, operation_id)
    if operation is None or operation.workflow_id != workflow.workflow_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Workflow operation not found"
        )
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
        workflow = _require_workflow_access(
            session=session, principal=principal, execution_id=execution_id
        )
        operation = session.get(WorkflowOperation, operation_id)
        if operation is None or operation.workflow_id != workflow.workflow_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Workflow operation not found",
            )
        normalized_operation_id = operation.operation_id
        if normalized_attempt_id is not None:
            attempt = session.get(WorkflowOperationAttempt, normalized_attempt_id)
            if attempt is None or attempt.operation_id != normalized_operation_id:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Workflow operation attempt not found",
                )
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
    _require_workflow_access(
        session=session, principal=principal, execution_id=execution_id
    )
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
    _require_workflow_access(
        session=session, principal=principal, execution_id=execution_id
    )
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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found"
        )
    if not principal.is_platform_super_admin:
        require_tenant_workspace_access(
            principal=principal, tenant_id=workflow.tenant_id
        )
    return workflow
