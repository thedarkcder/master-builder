from __future__ import annotations

from fastapi import HTTPException, status

from orchestrator.api.admin.workflows.attempt_service import create_workflow_attempt, resume_workflow_execution
from orchestrator.api.admin.workflows.events_service import (
    get_workflow_step_audit_attempt,
    get_workflow_step_transcript,
    list_workflow_audit_events,
    list_workflow_telemetry_events,
)
from orchestrator.api.admin.workflows.execution_read_service import list_workflow_schemas, workflow_schema
from orchestrator.api.admin.workflows.operation_retry_service import retry_workflow_operation
from orchestrator.api.admin.workflows.operation_restart_service import restart_workflow_operation
from orchestrator.api.admin.workflows.queries import workflow_by_execution_id
from orchestrator.api.admin.workflows.start_development_service import (
    preview_start_engineering,
    start_engineering_from_action,
    start_work_result_to_schema,
)
from orchestrator.api.admin.workflows.type_read_model import list_workflow_type_summaries, workflow_type_detail
from orchestrator.core.workflow.type_catalog import get_workflow_type

__all__ = [
    "create_workflow_attempt",
    "get_workflow",
    "get_workflow_step_audit_attempt",
    "get_workflow_step_transcript",
    "get_workflow_type_detail",
    "list_workflow_audit_events",
    "list_workflow_telemetry_events",
    "list_workflow_types",
    "list_workflows",
    "resume_workflow_execution",
    "restart_workflow_operation",
    "retry_workflow_operation",
    "preview_start_engineering",
    "start_engineering_from_action",
    "start_work_result_to_schema",
]


def list_workflow_types(
    *,
    session,
    tenant_id: str | None,
):  # noqa: ANN001
    return list_workflow_type_summaries(session=session, tenant_id=tenant_id)


def get_workflow_type_detail(
    *,
    session,
    workflow_type_key: str,
    tenant_id: str | None,
):  # noqa: ANN001
    workflow_type = get_workflow_type(session, workflow_type_key=workflow_type_key)
    return workflow_type_detail(session=session, workflow_type=workflow_type, tenant_id=tenant_id)


def list_workflows(
    *,
    session,
    tenant_id: str | None,
    project_id: str | None,
    status_filter: str | None,
    issue_query: str | None,
    limit: int,
    offset: int,
    workflow_to_schema_fn,
    run_to_schema_fn,
):  # noqa: ANN001
    return list_workflow_schemas(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        status_filter=status_filter,
        issue_query=issue_query,
        limit=limit,
        offset=offset,
        workflow_to_schema_fn=workflow_to_schema_fn,
        run_to_schema_fn=run_to_schema_fn,
    )


def get_workflow(
    *,
    session,
    execution_id: str,
    workflow_to_schema_fn,
    run_to_schema_fn,
):  # noqa: ANN001
    workflow = workflow_by_execution_id(session=session, execution_id=execution_id)
    if workflow is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found")
    return workflow_schema(
        session=session,
        workflow=workflow,
        workflow_to_schema_fn=workflow_to_schema_fn,
        run_to_schema_fn=run_to_schema_fn,
    )
