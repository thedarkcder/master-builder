from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.storage.models import ProjectInstall

INSTALL_KIND_FASTLANE = "fastlane_lane"
INSTALL_KIND_INTEGRATION = "integration"
INSTALL_KIND_SUPABASE = "supabase_command"
INSTALL_KIND_RAILWAY = "railway_command"
INSTALL_KIND_SLACK = "slack_action"
SUPPORTED_INSTALL_KINDS = (
    INSTALL_KIND_FASTLANE,
    INSTALL_KIND_INTEGRATION,
    INSTALL_KIND_SUPABASE,
    INSTALL_KIND_RAILWAY,
    INSTALL_KIND_SLACK,
)


@dataclass(frozen=True)
class ProjectInstallWrite:
    kind: str
    label: str
    enabled: bool
    config: dict
    binding_names: tuple[str, ...]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_install_kind(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in SUPPORTED_INSTALL_KINDS:
        raise ValueError(f"Unsupported install kind '{value}'")
    return normalized


def normalize_binding_names(
    values: list[str] | tuple[str, ...] | set[str] | None,
) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_value in values or ():
        value = str(raw_value or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return tuple(normalized)


def normalize_install_write(payload: ProjectInstallWrite) -> ProjectInstallWrite:
    kind = normalize_install_kind(payload.kind)
    label = str(payload.label or "").strip()
    if not label:
        raise ValueError("Install label is required")
    config = payload.config if isinstance(payload.config, dict) else {}
    return ProjectInstallWrite(
        kind=kind,
        label=label,
        enabled=bool(payload.enabled),
        config=dict(config),
        binding_names=normalize_binding_names(payload.binding_names),
    )


def list_project_installs(
    *, session: Session, tenant_id: str, project_id: str
) -> list[ProjectInstall]:
    return (
        session.execute(
            select(ProjectInstall)
            .where(
                ProjectInstall.tenant_id == tenant_id,
                ProjectInstall.project_id == project_id,
            )
            .order_by(ProjectInstall.label.asc(), ProjectInstall.created_at.asc())
        )
        .scalars()
        .all()
    )


def get_project_install(*, session: Session, install_id: str) -> ProjectInstall | None:
    return session.get(ProjectInstall, install_id)


def create_project_install(
    *,
    session: Session,
    tenant_id: str,
    project_id: str,
    payload: ProjectInstallWrite,
    now: datetime | None = None,
) -> ProjectInstall:
    timestamp = now or _utc_now()
    normalized = normalize_install_write(payload)
    install = ProjectInstall(
        install_id=uuid4().hex,
        tenant_id=tenant_id,
        project_id=project_id,
        kind=normalized.kind,
        label=normalized.label,
        enabled=normalized.enabled,
        config_json=normalized.config,
        binding_names_json=list(normalized.binding_names),
        created_at=timestamp,
        updated_at=timestamp,
    )
    session.add(install)
    session.commit()
    session.refresh(install)
    return install


def update_project_install(
    *,
    session: Session,
    install: ProjectInstall,
    payload: ProjectInstallWrite,
    now: datetime | None = None,
) -> ProjectInstall:
    timestamp = now or _utc_now()
    normalized = normalize_install_write(payload)
    install.kind = normalized.kind
    install.label = normalized.label
    install.enabled = normalized.enabled
    install.config_json = normalized.config
    install.binding_names_json = list(normalized.binding_names)
    install.updated_at = timestamp
    session.commit()
    session.refresh(install)
    return install


def delete_project_install(*, session: Session, install: ProjectInstall) -> None:
    session.delete(install)
    session.commit()
