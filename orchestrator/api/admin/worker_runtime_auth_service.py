from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.api.schemas import WorkerRuntimeAuthRequestRead
from orchestrator.core.runtime_requirements import normalize_runtime_kinds
from orchestrator.storage.models import WorkerRuntimeAuthRequest, WorkerRuntimeState

OPEN_WORKER_RUNTIME_AUTH_REQUEST_STATUSES = {"pending", "active"}


def start_worker_runtime_auth_request(
    *,
    session: Session,
    service_instance_id: str,
    runtime_kind: str,
) -> WorkerRuntimeAuthRequestRead:
    normalized_service_instance_id = str(service_instance_id or "").strip()
    normalized_runtime_kind = str(runtime_kind or "").strip().lower()
    if not normalized_service_instance_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="service_instance_id is required")
    runtime_kinds = normalize_runtime_kinds((normalized_runtime_kind,))
    if not runtime_kinds:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="runtime_kind is invalid")
    normalized_runtime_kind = runtime_kinds[0]
    worker_row = session.get(WorkerRuntimeState, normalized_service_instance_id)
    if worker_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Worker instance was not found")
    registered_runtime_kinds = set(normalize_runtime_kinds(getattr(worker_row, "runtime_kinds_json", None)))
    if normalized_runtime_kind not in registered_runtime_kinds:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Worker instance is not registered for runtime '{normalized_runtime_kind}'",
        )

    now = datetime.now(timezone.utc)
    open_requests = session.execute(
        select(WorkerRuntimeAuthRequest)
        .where(
            WorkerRuntimeAuthRequest.service_instance_id == normalized_service_instance_id,
            WorkerRuntimeAuthRequest.runtime_kind == normalized_runtime_kind,
            WorkerRuntimeAuthRequest.status.in_(tuple(sorted(OPEN_WORKER_RUNTIME_AUTH_REQUEST_STATUSES))),
        )
        .order_by(WorkerRuntimeAuthRequest.requested_at.desc())
    ).scalars().all()
    for request in open_requests:
        request.status = "cancelled"
        request.completed_at = now
        request.last_error = "Superseded by a newer login session request."

    auth_request = WorkerRuntimeAuthRequest(
        request_id=str(uuid4()),
        service_instance_id=normalized_service_instance_id,
        runtime_kind=normalized_runtime_kind,
        status="pending",
        remediation_text=None,
        requested_at=now,
        started_at=None,
        completed_at=None,
        expires_at=None,
        last_error=None,
    )
    session.add(auth_request)
    session.commit()
    session.refresh(auth_request)
    return WorkerRuntimeAuthRequestRead.model_validate(auth_request, from_attributes=True)


def get_worker_runtime_auth_request(
    *,
    session: Session,
    request_id: str,
) -> WorkerRuntimeAuthRequestRead:
    normalized_request_id = str(request_id or "").strip()
    if not normalized_request_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="request_id is required")
    auth_request = session.get(WorkerRuntimeAuthRequest, normalized_request_id)
    if auth_request is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Worker runtime auth request was not found")
    return WorkerRuntimeAuthRequestRead.model_validate(auth_request, from_attributes=True)
