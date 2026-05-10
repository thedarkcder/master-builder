from __future__ import annotations

from sqlalchemy.orm import Session

from orchestrator.api.schemas import WorkerRuntimeAuthRequestRead
from orchestrator.core.worker.runtime_status_service import (
    get_worker_runtime_login_request,
    start_worker_runtime_login_request,
)


def start_worker_runtime_auth_request(
    *,
    session: Session,
    service_instance_id: str,
    runtime_kind: str,
) -> WorkerRuntimeAuthRequestRead:
    request = start_worker_runtime_login_request(
        session=session,
        service_instance_id=service_instance_id,
        runtime_kind=runtime_kind,
    )
    return WorkerRuntimeAuthRequestRead.model_validate(request, from_attributes=True)


def get_worker_runtime_auth_request(
    *,
    session: Session,
    request_id: str,
) -> WorkerRuntimeAuthRequestRead:
    request = get_worker_runtime_login_request(session=session, request_id=request_id)
    return WorkerRuntimeAuthRequestRead.model_validate(request, from_attributes=True)
