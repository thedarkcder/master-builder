from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.storage.models import WorkflowType, WorkflowTypeOperation

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
    engine_config: dict | None,
    operation_updates: dict[str, dict[str, object]],
) -> WorkflowType:
    workflow_type = get_workflow_type(session, workflow_type_key=workflow_type_key)
    workflow_type.orchestration_backend = str(orchestration_backend or "").strip().lower()
    workflow_type.engine_config_json = dict(engine_config or {})
    if workflow_type.orchestration_backend == "temporal":
        from orchestrator.temporal.workflow_registry import resolve_temporal_workflow_definition

        temporal = workflow_type.engine_config_json.get("temporal")
        if not isinstance(temporal, dict):
            raise ValueError("Temporal workflows require engine_config.temporal")
        workflow_name = str(temporal.get("workflow_name") or "").strip()
        try:
            resolve_temporal_workflow_definition(workflow_name=workflow_name)
        except LookupError as exc:
            raise ValueError(str(exc)) from exc
    else:
        workflow_type.engine_config_json = {}

    definitions = list_workflow_type_operations(session, workflow_type_key=workflow_type.workflow_type_key)
    definitions_by_type = {
        str(definition.operation_type or "").strip(): definition
        for definition in definitions
        if str(definition.operation_type or "").strip()
    }
    unknown_operations = sorted(set(operation_updates) - set(definitions_by_type))
    if unknown_operations:
        raise ValueError(f"Workflow type update referenced unknown operations: {', '.join(unknown_operations)}")

    for operation_type, update in operation_updates.items():
        definition = definitions_by_type[operation_type]
        retry_policy = str(update.get("retry_policy") or "").strip()
        if not retry_policy:
            raise ValueError(f"Workflow type operation {operation_type} requires retry_policy")
        definition.retry_policy = retry_policy
        definition.retry_policy_config_json = dict(update.get("retry_policy_config") or {})
    return workflow_type
