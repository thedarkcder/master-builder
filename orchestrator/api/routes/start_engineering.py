from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from orchestrator.api.admin.workflows import use_cases
from orchestrator.api.dependencies import get_session
from orchestrator.api.schemas import (
    StartEngineeringPreviewRead,
    WorkflowStartWorkRead,
    WorkflowStartWorkRequest,
)
from orchestrator.core.security import (
    AuthenticatedPrincipal,
    require_authenticated_principal,
)

router = APIRouter(prefix="/api/app/start-engineering", tags=["start-engineering"])


@router.get("/{execution_id}/preview", response_model=StartEngineeringPreviewRead)
def preview_start_engineering(
    execution_id: str,
    action_token: str = Query(...),
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> StartEngineeringPreviewRead:
    return use_cases.preview_start_engineering(
        session=session,
        principal=principal,
        execution_id=execution_id,
        action_token=action_token,
    )


@router.post("/{execution_id}/start", response_model=WorkflowStartWorkRead)
def start_engineering(
    execution_id: str,
    payload: WorkflowStartWorkRequest,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> WorkflowStartWorkRead:
    return use_cases.start_engineering_from_action(
        session=session,
        principal=principal,
        execution_id=execution_id,
        action_token=payload.action_token,
    )
