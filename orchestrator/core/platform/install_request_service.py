from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.core.platform.install_registry_service import SUPPORTED_INSTALL_KINDS
from orchestrator.core.runs.human_input_service import create_human_input_request
from orchestrator.storage.models import Project, ProjectInstallRequest, Run, Tenant

INSTALL_REQUEST_STATUS_PENDING = "pending"
INSTALL_REQUEST_STATUS_FULFILLED = "fulfilled"
INSTALL_REQUEST_STATUS_REJECTED = "rejected"

INSTALL_REQUEST_KIND_PROJECT_MISSING = "project_missing_install"
INSTALL_REQUEST_KIND_UNSUPPORTED = "unsupported_kind"


@dataclass(frozen=True)
class ProjectInstallRequestWrite:
    kind: str
    label: str
    reason: str
    suggested_config: dict
    required_bindings: tuple[str, ...]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_bindings(values: tuple[str, ...] | list[str] | set[str] | None) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_value in values or ():
        value = str(raw_value or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return tuple(normalized)


def normalize_install_request_write(payload: ProjectInstallRequestWrite) -> ProjectInstallRequestWrite:
    kind = str(payload.kind or "").strip().lower()
    label = str(payload.label or "").strip()
    reason = str(payload.reason or "").strip()
    if not kind:
        raise ValueError("Install kind is required")
    if not label:
        raise ValueError("Install label is required")
    if not reason:
        raise ValueError("Install reason is required")
    return ProjectInstallRequestWrite(
        kind=kind,
        label=label,
        reason=reason,
        suggested_config=payload.suggested_config if isinstance(payload.suggested_config, dict) else {},
        required_bindings=_normalize_bindings(payload.required_bindings),
    )


def request_kind_for_install(kind: str) -> str:
    normalized_kind = str(kind or "").strip().lower()
    if normalized_kind in SUPPORTED_INSTALL_KINDS:
        return INSTALL_REQUEST_KIND_PROJECT_MISSING
    return INSTALL_REQUEST_KIND_UNSUPPORTED


def list_project_install_requests(
    *,
    session: Session,
    tenant_id: str,
    project_id: str,
    statuses: tuple[str, ...] | None = None,
) -> list[ProjectInstallRequest]:
    query = (
        select(ProjectInstallRequest)
        .where(
            ProjectInstallRequest.tenant_id == tenant_id,
            ProjectInstallRequest.project_id == project_id,
        )
        .order_by(ProjectInstallRequest.created_at.desc())
    )
    if statuses:
        query = query.where(ProjectInstallRequest.status.in_(tuple(statuses)))
    return session.execute(query).scalars().all()


def create_install_request(
    *,
    session: Session,
    settings,
    tenant: Tenant,
    project: Project,
    run: Run,
    source_stage: str,
    payload: ProjectInstallRequestWrite,
    now: datetime | None = None,
) -> ProjectInstallRequest:
    normalized = normalize_install_request_write(payload)
    request_kind = request_kind_for_install(normalized.kind)
    existing = (
        session.execute(
            select(ProjectInstallRequest)
            .where(
                ProjectInstallRequest.tenant_id == tenant.tenant_id,
                ProjectInstallRequest.project_id == project.project_id,
                ProjectInstallRequest.workflow_id == run.workflow_id,
                ProjectInstallRequest.kind == normalized.kind,
                ProjectInstallRequest.label == normalized.label,
                ProjectInstallRequest.status == INSTALL_REQUEST_STATUS_PENDING,
            )
            .order_by(ProjectInstallRequest.created_at.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )
    if existing is not None:
        return existing

    timestamp = now or _utc_now()
    request = ProjectInstallRequest(
        request_id=uuid4().hex,
        tenant_id=tenant.tenant_id,
        project_id=project.project_id,
        workflow_id=run.workflow_id,
        run_id=run.run_id,
        issue_key=str(run.issue_key or "").strip(),
        kind=normalized.kind,
        label=normalized.label,
        reason=normalized.reason,
        suggested_config_json=normalized.suggested_config,
        required_bindings_json=list(normalized.required_bindings),
        status=INSTALL_REQUEST_STATUS_PENDING,
        request_kind=request_kind,
        created_at=timestamp,
        updated_at=timestamp,
    )
    session.add(request)
    human_prompt = (
        f"Install request for `{run.issue_key}`: add the `{normalized.kind}` integration named "
        f"`{normalized.label}` to project `{project.name}`."
    )
    instructions = (
        "Configure the install in the project Installs admin page, attach the required bindings, "
        "then reply in this thread with `ready`."
    )
    if request_kind == INSTALL_REQUEST_KIND_UNSUPPORTED:
        instructions = (
            "This integration kind is not yet supported by the platform. Review the request in the "
            "project Installs admin page and reply in this thread once platform support exists."
        )
    create_human_input_request(
        session=session,
        settings=settings,
        tenant=tenant,
        project=project,
        run=run,
        issue_key=run.issue_key,
        source_stage=source_stage,
        request_type="install_request",
        prompt=human_prompt,
        instructions=instructions,
        expected_reply_format="Reply with `ready` after the install has been configured.",
        request_context={
            "install_request_id": request.request_id,
            "kind": normalized.kind,
            "label": normalized.label,
            "request_kind": request_kind,
            "required_bindings": list(normalized.required_bindings),
        },
        expires_in_minutes=60,
    )
    session.refresh(request)
    return request


def get_project_install_request(*, session: Session, request_id: str) -> ProjectInstallRequest | None:
    return session.get(ProjectInstallRequest, request_id)


def update_install_request_status(
    *,
    session: Session,
    request: ProjectInstallRequest,
    status: str,
    now: datetime | None = None,
) -> ProjectInstallRequest:
    normalized_status = str(status or "").strip().lower()
    if normalized_status not in {
        INSTALL_REQUEST_STATUS_PENDING,
        INSTALL_REQUEST_STATUS_FULFILLED,
        INSTALL_REQUEST_STATUS_REJECTED,
    }:
        raise ValueError(f"Unsupported install request status '{status}'")
    request.status = normalized_status
    request.updated_at = now or _utc_now()
    session.commit()
    session.refresh(request)
    return request
