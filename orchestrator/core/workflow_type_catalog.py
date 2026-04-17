from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.storage.models import WorkflowType, WorkflowTypeOperation


def get_workflow_type(session: Session, *, workflow_type_key: str) -> WorkflowType:
    workflow_type = session.get(WorkflowType, str(workflow_type_key or "").strip())
    if workflow_type is None:
        raise LookupError(f"Workflow type not found: {workflow_type_key}")
    return workflow_type


def list_workflow_type_operations(session: Session, *, workflow_type_key: str) -> list[WorkflowTypeOperation]:
    return session.execute(
        select(WorkflowTypeOperation)
        .where(WorkflowTypeOperation.workflow_type_key == str(workflow_type_key or "").strip())
        .order_by(WorkflowTypeOperation.sort_order.asc())
    ).scalars().all()
