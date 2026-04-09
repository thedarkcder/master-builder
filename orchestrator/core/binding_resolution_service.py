from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from orchestrator.core.platform_secret_service import resolve_platform_secret_ref
from orchestrator.core.secret_manager import normalize_secret_ref
from orchestrator.core.tenant_secret_service import resolve_scoped_secret_ref
from orchestrator.storage.models import Project


@dataclass(frozen=True)
class BindingStatus:
    key: str
    present: bool
    source: str


def normalize_binding_names(keys: list[str] | tuple[str, ...] | set[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_key in keys:
        key = str(raw_key or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        normalized.append(key)
    return normalized


def _project_binding_maps(project: Project) -> tuple[dict[str, str], dict[str, str]]:
    environment_map = project.environment if isinstance(project.environment, dict) else {}
    secret_ref_map = project.secret_refs if isinstance(project.secret_refs, dict) else {}
    return (
        {
            str(key).strip(): str(value or "")
            for key, value in environment_map.items()
            if str(key).strip()
        },
        {
            str(key).strip(): str(value or "").strip()
            for key, value in secret_ref_map.items()
            if str(key).strip() and str(value or "").strip()
        },
    )


def _resolve_project_secret_ref(
    *,
    session: Session,
    tenant_id: str,
    project_id: str,
    encryption_key: str,
    secret_ref: str,
) -> str | None:
    normalized_ref = normalize_secret_ref(secret_ref)
    if normalized_ref.startswith("platform/"):
        return resolve_platform_secret_ref(
            session,
            secret_ref=normalized_ref,
            encryption_key=encryption_key,
        )
    return resolve_scoped_secret_ref(
        session,
        secret_ref=normalized_ref,
        tenant_id=tenant_id,
        project_id=project_id,
        encryption_key=encryption_key,
    )


def check_project_bindings(
    *,
    session: Session,
    project: Project,
    tenant_id: str,
    encryption_key: str,
    keys: list[str] | tuple[str, ...] | set[str],
) -> list[BindingStatus]:
    normalized_keys = normalize_binding_names(keys)
    environment_map, secret_ref_map = _project_binding_maps(project)
    statuses: list[BindingStatus] = []
    for key in normalized_keys:
        if key in environment_map:
            statuses.append(BindingStatus(key=key, present=bool(environment_map[key]), source="environment"))
            continue
        secret_ref = secret_ref_map.get(key)
        if not secret_ref:
            statuses.append(BindingStatus(key=key, present=False, source="missing"))
            continue
        resolved_value = _resolve_project_secret_ref(
            session=session,
            tenant_id=tenant_id,
            project_id=project.project_id,
            encryption_key=encryption_key,
            secret_ref=secret_ref,
        )
        statuses.append(BindingStatus(key=key, present=resolved_value is not None, source="secret_ref"))
    return statuses


def resolve_project_binding_values(
    *,
    session: Session,
    project: Project,
    tenant_id: str,
    encryption_key: str,
    keys: list[str] | tuple[str, ...] | set[str],
) -> dict[str, str]:
    normalized_keys = normalize_binding_names(keys)
    environment_map, secret_ref_map = _project_binding_maps(project)
    values: dict[str, str] = {}
    for key in normalized_keys:
        if key in environment_map:
            values[key] = environment_map[key]
            continue
        secret_ref = secret_ref_map.get(key)
        if not secret_ref:
            raise ValueError(f"Binding '{key}' is not configured for project '{project.project_id}'")
        resolved_value = _resolve_project_secret_ref(
            session=session,
            tenant_id=tenant_id,
            project_id=project.project_id,
            encryption_key=encryption_key,
            secret_ref=secret_ref,
        )
        if resolved_value is None:
            raise ValueError(f"Binding '{key}' could not be resolved from secret ref '{secret_ref}'")
        values[key] = resolved_value
    return values
