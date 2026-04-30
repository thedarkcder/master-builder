from __future__ import annotations

from sqlalchemy import desc, func, select

from orchestrator.api.admin.schema_mappers import workflow_operation_to_schema
from orchestrator.api.admin.workflow_execution_state_read_model import build_workflow_execution_state_read_model
from orchestrator.api.admin.workflow_queries import (
    audit_events_by_operation,
    latest_run_for_workflow,
    pending_input_request,
    workflow_checkpoint_kinds,
    workflow_operation_attempts_by_operation,
    workflow_operations,
    workflow_runs,
)
from orchestrator.api.schemas import (
    WorkflowExecutionPreviewRead,
    WorkflowOperationRead,
    WorkflowRetryPolicyRead,
    WorkflowTypeDetailRead,
    WorkflowTypeLifecycleRead,
    WorkflowTypeOperationRead,
    WorkflowTypeRead,
    WorkflowTypeSummaryRead,
)
from orchestrator.core.product_events import ProductEvent
from orchestrator.core.workflow_definition import WorkflowDefinition, WorkflowStepDefinition
from orchestrator.core.workflow_handler_composition import installed_operation_retry_capabilities
from orchestrator.core.workflow_type_catalog import get_workflow_type, list_workflow_types
from orchestrator.storage.models import WorkflowExecution, WorkflowOperation, WorkflowOperationAttempt


def workflow_retry_policy_read(*, raw_config: dict | None) -> WorkflowRetryPolicyRead:
    raw = raw_config if isinstance(raw_config, dict) else {}
    return WorkflowRetryPolicyRead(
        manual_retry_enabled=bool(raw.get("manual_retry_enabled", True)),
        max_attempts=int(raw.get("max_attempts") or 1),
        initial_interval_seconds=int(raw.get("initial_interval_seconds") or 0),
        max_interval_seconds=int(raw.get("max_interval_seconds") or 0),
        backoff_coefficient=float(raw.get("backoff_coefficient") or 1.0),
    )


def workflow_supports_child_issue_links(*, workflow_type: WorkflowTypeRead) -> bool:
    return bool(workflow_type.capabilities.get("child_issue_links"))


def operation_status_by_type(*, operations: list[WorkflowOperation]) -> dict[str, WorkflowOperation]:
    return {
        str(operation.operation_type or "").strip(): operation
        for operation in operations
        if str(operation.operation_type or "").strip()
    }


def _workflow_type_read(
    *,
    workflow_type: WorkflowDefinition,
    operations: list[WorkflowOperation] | None = None,
    executable_retry_operation_types: frozenset[str] | None = None,
) -> WorkflowTypeRead:
    status_by_type = operation_status_by_type(operations=operations or [])
    executable_retry_types = (
        frozenset(
            capability.operation_type
            for capability in installed_operation_retry_capabilities(workflow_type=workflow_type)
        )
        if executable_retry_operation_types is None
        else executable_retry_operation_types
    )
    operation_reads = [
        WorkflowTypeOperationRead(
            operation_type=definition.key,
            label=definition.label,
            description=definition.description,
            completion_required=bool(definition.required),
            kind=definition.kind.value,
            after=list(definition.after),
            supports=list(definition.supports),
            required=bool(definition.required),
            retryable=bool(definition.retryable and definition.key in executable_retry_types),
            graph_index=definition.graph_index,
            status=(status_by_type[definition.key].status if definition.key in status_by_type else _default_operation_status(definition.key, None)),
        )
        for definition in workflow_type.steps
    ]
    return WorkflowTypeRead(
        key=workflow_type.workflow_type_key,
        label=workflow_type.label,
        description=workflow_type.description,
        orchestration_backend=workflow_type.orchestration_backend,
        retry_policy=workflow_retry_policy_read(raw_config=workflow_type.retry_policy.to_payload()),
        capabilities=dict(workflow_type.capabilities),
        lifecycle=WorkflowTypeLifecycleRead(state_path_kind="operation"),
        operations=operation_reads,
    )


def _default_operation_status(operation_type: str, workflow: WorkflowExecution | None) -> str:
    if operation_type == "run_attempt_execution" and workflow is not None:
        return str(workflow.status or "").strip() or "pending"
    return "pending"


def _definition_by_key(workflow_type: WorkflowDefinition) -> dict[str, WorkflowStepDefinition]:
    return {definition.key: definition for definition in workflow_type.steps}


def workflow_operation_reads(
    *,
    session,
    workflow: WorkflowExecution,
    operations: list[WorkflowOperation],
    operation_attempts: dict[str, list[WorkflowOperationAttempt]],
    operation_events: dict[str, list[ProductEvent]],
) -> tuple[WorkflowTypeRead, list[WorkflowOperationRead]]:
    workflow_type = get_workflow_type(session, workflow_type_key=workflow.workflow_type_key)
    status_by_type = operation_status_by_type(operations=operations)
    definitions_by_key = _definition_by_key(workflow_type)
    executable_retry_types = frozenset(
        capability.operation_type
        for capability in installed_operation_retry_capabilities(workflow_type=workflow_type)
    )

    def operation_retry_state(
        *,
        definition: WorkflowStepDefinition,
        current: WorkflowOperation | None,
    ) -> tuple[bool, str | None]:
        if current is None:
            return False, "Operation has not started yet."
        attempts = operation_attempts.get(current.operation_id, [])
        latest_attempt = attempts[-1] if attempts else None
        if latest_attempt is None:
            return False, "Operation has no attempt history to retry."
        if not definition.retryable:
            return False, "Manual retry is disabled by the workflow definition."
        if definition.key not in executable_retry_types:
            return False, "No executable retry handler is registered for this operation."
        if str(latest_attempt.status or "").strip().lower() not in {"failed", "retrying"}:
            return False, "Latest attempt is not in a failed state."
        return True, None

    workflow_type_read = _workflow_type_read(
        workflow_type=workflow_type,
        operations=operations,
        executable_retry_operation_types=executable_retry_types,
    )
    operation_reads: list[WorkflowOperationRead] = []
    for definition in workflow_type.steps:
        current = status_by_type.get(definition.key)
        can_retry, retry_unavailable_reason = operation_retry_state(
            definition=definition,
            current=current,
        )
        operation_reads.append(
            workflow_operation_to_schema(
                current,
                operation_id=(current.operation_id if current is not None else f"{workflow.workflow_id}:{definition.key}"),
                operation_type=definition.key,
                status=(current.status if current is not None else _default_operation_status(definition.key, workflow)),
                label=definition.label,
                description=definition.description,
                required=bool(definition.required),
                definition_only=current is None,
                attempts=(operation_attempts.get(current.operation_id, []) if current is not None else []),
                events=(operation_events.get(current.operation_id, []) if current is not None else []),
                can_retry=can_retry,
                retry_unavailable_reason=retry_unavailable_reason,
                kind=definition.kind.value,
                after=list(definition.after),
                supports=list(definition.supports),
            )
        )

    undefined_operation_types = sorted(
        {
            str(operation.operation_type or "").strip()
            for operation in operations
            if str(operation.operation_type or "").strip()
            and str(operation.operation_type or "").strip() not in definitions_by_key
        }
    )
    if undefined_operation_types:
        raise ValueError(
            "Workflow execution references undefined operations for "
            f"{workflow.workflow_type_key}: {', '.join(undefined_operation_types)}"
        )

    return workflow_type_read, operation_reads


def workflow_execution_preview(*, session, workflow: WorkflowExecution) -> WorkflowExecutionPreviewRead:  # noqa: ANN001
    operations = workflow_operations(session=session, workflow_id=workflow.workflow_id)
    pending_request = pending_input_request(session=session, workflow_id=workflow.workflow_id)
    workflow_type, _ = workflow_operation_reads(
        session=session,
        workflow=workflow,
        operations=operations,
        operation_attempts=workflow_operation_attempts_by_operation(session=session, workflow_id=workflow.workflow_id),
        operation_events=audit_events_by_operation(session=session, workflow_id=workflow.workflow_id),
    )
    workflow_state = build_workflow_execution_state_read_model(
        workflow=workflow,
        workflow_type=workflow_type,
        latest_run=latest_run_for_workflow(session=session, workflow_id=workflow.workflow_id),
        operations=operations,
        pending_request=pending_request,
        checkpoint_kinds=workflow_checkpoint_kinds(session=session, workflow_id=workflow.workflow_id),
        runs=workflow_runs(session=session, workflow_id=workflow.workflow_id),
    )
    return WorkflowExecutionPreviewRead(
        execution_id=workflow.execution_id,
        workflow_id=workflow.workflow_id,
        source_system=workflow.source_system,
        source_ref=workflow.source_ref,
        display_name=workflow.display_name,
        status=workflow.status,
        waiting_on=workflow_state.waiting_on,
        next_step=workflow_state.next_step,
        failure_reason=workflow.last_error,
        created_at=workflow.created_at,
        finished_at=workflow.finished_at,
    )


def workflow_type_detail(
    *,
    session,
    workflow_type: WorkflowDefinition,
    tenant_id: str | None,
) -> WorkflowTypeDetailRead:  # noqa: ANN001
    type_read = _workflow_type_read(workflow_type=workflow_type)
    execution_query = (
        select(WorkflowExecution)
        .where(WorkflowExecution.workflow_type_key == workflow_type.workflow_type_key)
        .order_by(desc(WorkflowExecution.created_at))
    )
    count_query = select(func.count()).select_from(WorkflowExecution).where(
        WorkflowExecution.workflow_type_key == workflow_type.workflow_type_key
    )
    latest_execution_query = select(func.max(WorkflowExecution.created_at)).where(
        WorkflowExecution.workflow_type_key == workflow_type.workflow_type_key
    )
    if tenant_id:
        execution_query = execution_query.where(WorkflowExecution.tenant_id == tenant_id)
        count_query = count_query.where(WorkflowExecution.tenant_id == tenant_id)
        latest_execution_query = latest_execution_query.where(WorkflowExecution.tenant_id == tenant_id)

    recent_executions = [
        workflow_execution_preview(session=session, workflow=execution)
        for execution in session.execute(execution_query.limit(20)).scalars().all()
    ]
    return WorkflowTypeDetailRead(
        key=type_read.key,
        label=type_read.label,
        description=type_read.description,
        orchestration_backend=type_read.orchestration_backend,
        retry_policy=type_read.retry_policy,
        capabilities=type_read.capabilities,
        lifecycle=type_read.lifecycle,
        operations=type_read.operations,
        execution_count=int(session.execute(count_query).scalar_one()),
        latest_execution_at=session.execute(latest_execution_query).scalar_one(),
        recent_executions=recent_executions,
    )


def list_workflow_type_summaries(*, session, tenant_id: str | None) -> list[WorkflowTypeSummaryRead]:  # noqa: ANN001
    result: list[WorkflowTypeSummaryRead] = []
    for workflow_type in list_workflow_types(session):
        count_query = select(func.count()).select_from(WorkflowExecution).where(
            WorkflowExecution.workflow_type_key == workflow_type.workflow_type_key
        )
        latest_query = select(func.max(WorkflowExecution.created_at)).where(
            WorkflowExecution.workflow_type_key == workflow_type.workflow_type_key
        )
        if tenant_id:
            count_query = count_query.where(WorkflowExecution.tenant_id == tenant_id)
            latest_query = latest_query.where(WorkflowExecution.tenant_id == tenant_id)
        result.append(
            WorkflowTypeSummaryRead(
                key=workflow_type.workflow_type_key,
                label=workflow_type.label,
                description=workflow_type.description,
                operation_count=len(workflow_type.steps),
                execution_count=int(session.execute(count_query).scalar_one()),
                latest_execution_at=session.execute(latest_query).scalar_one(),
            )
        )
    return result
