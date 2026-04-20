from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.storage.models import WorkflowType, WorkflowTypeOperation


def normalize_workflow_retry_policy_config(raw: dict | None) -> dict[str, object]:
    config = raw if isinstance(raw, dict) else {}
    return {
        "manual_retry_enabled": bool(config.get("manual_retry_enabled", True)),
        "max_attempts": max(1, int(config.get("max_attempts") or 1)),
        "initial_interval_seconds": max(0, int(config.get("initial_interval_seconds") or 0)),
        "max_interval_seconds": max(0, int(config.get("max_interval_seconds") or 0)),
        "backoff_coefficient": max(1.0, float(config.get("backoff_coefficient") or 1.0)),
    }

def get_workflow_type(session: Session, *, workflow_type_key: str) -> WorkflowType:
    workflow_type = session.get(WorkflowType, str(workflow_type_key or "").strip())
    if workflow_type is None:
        raise LookupError(f"Workflow type not found: {workflow_type_key}")
    return workflow_type


def get_workflow_type_by_system_key(session: Session, *, system_key: str) -> WorkflowType:
    workflow_type = session.execute(
        select(WorkflowType).where(WorkflowType.system_key == str(system_key or "").strip())
    ).scalar_one_or_none()
    if workflow_type is None:
        raise LookupError(f"Workflow type not found for system key: {system_key}")
    return workflow_type


def get_workflow_type_by_handler_key(session: Session, *, handler_key: str) -> WorkflowType:
    workflow_type = session.execute(
        select(WorkflowType).where(WorkflowType.handler_key == str(handler_key or "").strip())
    ).scalar_one_or_none()
    if workflow_type is None:
        raise LookupError(f"Workflow type not found for handler key: {handler_key}")
    return workflow_type


def list_workflow_type_operations(session: Session, *, workflow_type_key: str) -> list[WorkflowTypeOperation]:
    return session.execute(
        select(WorkflowTypeOperation)
        .where(WorkflowTypeOperation.workflow_type_key == str(workflow_type_key or "").strip())
        .order_by(WorkflowTypeOperation.sort_order.asc())
    ).scalars().all()


def update_workflow_type_configuration(
    session: Session,
    *,
    workflow_type_key: str,
    orchestration_backend: str,
    retry_policy: dict[str, object] | None,
) -> WorkflowType:
    workflow_type = get_workflow_type(session, workflow_type_key=workflow_type_key)
    workflow_type.orchestration_backend = str(orchestration_backend or "").strip().lower()
    workflow_type.retry_policy_config_json = normalize_workflow_retry_policy_config(retry_policy)
    if workflow_type.orchestration_backend == "temporal":
        from orchestrator.temporal.workflow_registry import resolve_temporal_binding_for_handler

        try:
            resolve_temporal_binding_for_handler(handler_key=workflow_type.handler_key)
        except LookupError as exc:
            raise ValueError(str(exc)) from exc
    return workflow_type
