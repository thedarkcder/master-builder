from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from orchestrator.api.admin.deployment_host_service import (
    create_deployment_host as create_deployment_host_impl,
    get_deployment_host as get_deployment_host_impl,
    list_deployment_hosts as list_deployment_hosts_impl,
)
from orchestrator.api.dependencies import get_session
from orchestrator.api.schemas import DeploymentHostBootstrapRead, DeploymentHostCreate, DeploymentHostRead
from orchestrator.core.security import require_admin

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/deployment-hosts", response_model=list[DeploymentHostRead])
def list_deployment_hosts(
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> list[DeploymentHostRead]:
    return list_deployment_hosts_impl(session=session)


@router.post("/deployment-hosts", response_model=DeploymentHostBootstrapRead, status_code=status.HTTP_201_CREATED)
def create_deployment_host(
    payload: DeploymentHostCreate,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> DeploymentHostBootstrapRead:
    return create_deployment_host_impl(session=session, payload=payload)


@router.get("/deployment-hosts/{host_id}", response_model=DeploymentHostRead)
def get_deployment_host(
    host_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> DeploymentHostRead:
    return get_deployment_host_impl(session=session, host_id=host_id)
