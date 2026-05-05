from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from orchestrator.api.dependencies import get_session
from orchestrator.core.security import (
    AuthenticatedPrincipal,
    require_admin,
    require_authenticated_principal,
    require_authenticated_stream_principal,
)
from orchestrator.api.admin.workflows import use_cases
from orchestrator.api.schemas import (
    RunRead,
    WorkflowBoardItemRead,
    WorkflowAttemptCreateRequest,
    WorkflowExecutionStartRead,
    WorkflowExecutionStartRequest,
    WorkflowObservabilityEventRead,
    WorkflowOperationRestartRequest,
    WorkflowOperationRetryRead,
    WorkflowRead,
    WorkflowStepAttemptTranscriptRead,
    WorkflowStepTranscriptRead,
    WorkflowTypeDetailRead,
    WorkflowTypeSummaryRead,
)

router = APIRouter(prefix="/api/admin", tags=["admin"])

@router.get("/workflows", response_model=list[WorkflowRead])
def list_workflows(
    tenant_id: str | None = Query(default=None),
    project_id: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    issue_query: str | None = Query(default=None, alias="issue"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> list[WorkflowRead]:
    return use_cases.list_workflows(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        project_id=project_id,
        status_filter=status_filter,
        issue_query=issue_query,
        limit=limit,
        offset=offset,
    )


@router.get("/workflows/board", response_model=list[WorkflowBoardItemRead])
def list_workflow_board_items(
    tenant_id: str = Query(...),
    project_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> list[WorkflowBoardItemRead]:
    return use_cases.list_workflow_board_items(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        project_id=project_id,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/workflows",
    response_model=WorkflowExecutionStartRead,
    status_code=status.HTTP_201_CREATED,
)
def start_workflow_execution(
    payload: WorkflowExecutionStartRequest,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> WorkflowExecutionStartRead:
    return use_cases.start_workflow_execution(
        session=session,
        principal=principal,
        payload=payload,
    )


@router.get("/workflow-types", response_model=list[WorkflowTypeSummaryRead])
def list_workflow_types(
    tenant_id: str | None = Query(default=None),
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> list[WorkflowTypeSummaryRead]:
    return use_cases.list_workflow_types(session=session, principal=principal, tenant_id=tenant_id)

@router.get("/workflow-types/{workflow_type_key}", response_model=WorkflowTypeDetailRead)
def get_workflow_type_detail(
    workflow_type_key: str,
    tenant_id: str | None = Query(default=None),
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> WorkflowTypeDetailRead:
    return use_cases.get_workflow_type_detail(
        session=session,
        principal=principal,
        workflow_type_key=workflow_type_key,
        tenant_id=tenant_id,
    )

@router.get("/workflows/{execution_id}", response_model=WorkflowRead)
def get_workflow(
    execution_id: str,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> WorkflowRead:
    return use_cases.get_workflow(session=session, principal=principal, execution_id=execution_id)

@router.get("/workflows/{execution_id}/telemetry", response_model=list[WorkflowObservabilityEventRead])
def list_workflow_telemetry_events(
    execution_id: str,
    limit: int = Query(default=200, ge=1, le=500),
    before_recorded_at: datetime | None = Query(default=None),
    before_event_id: str | None = Query(default=None),
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> list[WorkflowObservabilityEventRead]:
    return use_cases.list_workflow_telemetry_events(
        session=session,
        principal=principal,
        execution_id=execution_id,
        limit=limit,
        before_recorded_at=before_recorded_at,
        before_event_id=before_event_id,
    )

@router.get("/workflows/{execution_id}/audit", response_model=list[WorkflowObservabilityEventRead])
def list_workflow_audit_events(
    execution_id: str,
    limit: int = Query(default=200, ge=1, le=500),
    before_recorded_at: datetime | None = Query(default=None),
    before_event_id: str | None = Query(default=None),
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> list[WorkflowObservabilityEventRead]:
    return use_cases.list_workflow_audit_events(
        session=session,
        principal=principal,
        execution_id=execution_id,
        limit=limit,
        before_recorded_at=before_recorded_at,
        before_event_id=before_event_id,
    )

@router.get("/workflows/{execution_id}/operations/{operation_id}/telemetry", response_model=list[WorkflowObservabilityEventRead])
def list_workflow_operation_telemetry_events(
    execution_id: str,
    operation_id: str,
    attempt_id: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> list[WorkflowObservabilityEventRead]:
    return use_cases.list_workflow_operation_telemetry_events(
        session=session,
        principal=principal,
        execution_id=execution_id,
        operation_id=operation_id,
        attempt_id=attempt_id,
        limit=limit,
    )

@router.get(
    "/workflows/{execution_id}/operations/{operation_id}/attempts/{attempt_id}/telemetry",
    response_model=list[WorkflowObservabilityEventRead],
)
def list_workflow_operation_attempt_telemetry_events(
    execution_id: str,
    operation_id: str,
    attempt_id: str,
    limit: int = Query(default=200, ge=1, le=500),
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> list[WorkflowObservabilityEventRead]:
    return use_cases.list_workflow_operation_telemetry_events(
        session=session,
        principal=principal,
        execution_id=execution_id,
        operation_id=operation_id,
        attempt_id=attempt_id,
        limit=limit,
    )

@router.get("/workflows/{execution_id}/operations/{operation_id}/telemetry/stream")
def stream_workflow_operation_telemetry_events(
    execution_id: str,
    operation_id: str,
    attempt_id: str | None = Query(default=None),
    after_event_sequence: int | None = Query(default=None, ge=0),
    principal: AuthenticatedPrincipal = Depends(require_authenticated_stream_principal),
) -> StreamingResponse:
    return StreamingResponse(
        use_cases.stream_workflow_operation_telemetry_events(
            principal=principal,
            execution_id=execution_id,
            operation_id=operation_id,
            attempt_id=attempt_id,
            after_event_sequence=after_event_sequence,
        ),
        media_type="application/x-ndjson",
    )

@router.get("/workflows/{execution_id}/operations/{operation_id}/attempts/{attempt_id}/telemetry/stream")
def stream_workflow_operation_attempt_telemetry_events(
    execution_id: str,
    operation_id: str,
    attempt_id: str,
    after_event_sequence: int | None = Query(default=None, ge=0),
    principal: AuthenticatedPrincipal = Depends(require_authenticated_stream_principal),
) -> StreamingResponse:
    return stream_workflow_operation_telemetry_events(
        execution_id=execution_id,
        operation_id=operation_id,
        attempt_id=attempt_id,
        after_event_sequence=after_event_sequence,
        principal=principal,
    )

@router.get("/workflows/{execution_id}/operations/{operation_id}/audit", response_model=list[WorkflowObservabilityEventRead])
def list_workflow_operation_audit_events(
    execution_id: str,
    operation_id: str,
    limit: int = Query(default=200, ge=1, le=500),
    before_recorded_at: datetime | None = Query(default=None),
    before_event_id: str | None = Query(default=None),
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> list[WorkflowObservabilityEventRead]:
    return use_cases.list_workflow_audit_events(
        session=session,
        principal=principal,
        execution_id=execution_id,
        operation_id=operation_id,
        limit=limit,
        before_recorded_at=before_recorded_at,
        before_event_id=before_event_id,
    )

@router.get("/workflows/{execution_id}/operations/{operation_id}/transcript", response_model=WorkflowStepTranscriptRead)
def get_workflow_operation_transcript(
    execution_id: str,
    operation_id: str,
    source: str = Query(default="telemetry"),
    attempt_id: str | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=2000),
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> WorkflowStepTranscriptRead:
    return use_cases.get_workflow_operation_transcript(
        session=session,
        principal=principal,
        execution_id=execution_id,
        operation_id=operation_id,
        source=source,
        attempt_id=attempt_id,
        limit=limit,
    )

@router.get(
    "/workflows/{execution_id}/operations/{operation_id}/attempts/{attempt_id}/audit",
    response_model=WorkflowStepAttemptTranscriptRead,
)
def get_workflow_operation_attempt_audit(
    execution_id: str,
    operation_id: str,
    attempt_id: str,
    limit: int = Query(default=500, ge=1, le=2000),
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> WorkflowStepAttemptTranscriptRead:
    return use_cases.get_workflow_operation_attempt_audit(
        session=session,
        principal=principal,
        execution_id=execution_id,
        operation_id=operation_id,
        attempt_id=attempt_id,
        limit=limit,
    )

@router.post("/workflows/{execution_id}/attempts", response_model=RunRead, status_code=status.HTTP_201_CREATED)
def create_workflow_attempt(
    execution_id: str,
    payload: WorkflowAttemptCreateRequest,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> RunRead:
    return use_cases.create_workflow_attempt(
        session=session,
        execution_id=execution_id,
        mode=payload.mode,
        checkpoint_kind=payload.checkpoint_kind,
    )

@router.post("/workflows/{execution_id}/resume", response_model=RunRead, status_code=status.HTTP_201_CREATED)
def resume_workflow_execution(
    execution_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> RunRead:
    return use_cases.resume_workflow_execution(session=session, execution_id=execution_id)


@router.post("/workflows/{execution_id}/operations/{operation_id}/retry", response_model=WorkflowOperationRetryRead)
def retry_workflow_operation(
    execution_id: str,
    operation_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> WorkflowOperationRetryRead:
    return use_cases.retry_workflow_operation(
        session=session,
        execution_id=execution_id,
        operation_id=operation_id,
    )


@router.post("/workflows/{execution_id}/operations/{operation_id}/restart", response_model=WorkflowOperationRetryRead)
def restart_workflow_operation(
    execution_id: str,
    operation_id: str,
    payload: WorkflowOperationRestartRequest,
    actor: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> WorkflowOperationRetryRead:
    return use_cases.restart_workflow_operation(
        session=session,
        execution_id=execution_id,
        operation_id=operation_id,
        actor=actor,
        payload=payload,
    )
