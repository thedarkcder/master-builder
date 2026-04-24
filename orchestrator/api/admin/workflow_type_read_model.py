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
    WorkflowTypeLifecycleStateRead,
    WorkflowTypeLifecycleTransitionRead,
    WorkflowTypeOperationRead,
    WorkflowTypeRead,
    WorkflowTypeSummaryRead,
)
from orchestrator.core.workflow_type_catalog import get_workflow_type, list_workflow_type_operations
from orchestrator.storage.models import AuditEvent, WorkflowExecution, WorkflowOperation, WorkflowOperationAttempt, WorkflowType


def workflow_type_capabilities_read(*, workflow_type: WorkflowType) -> dict[str, object]:
    raw = workflow_type.capabilities_json if isinstance(workflow_type.capabilities_json, dict) else {}
    return dict(raw)


def workflow_type_lifecycle_read(*, workflow_type: WorkflowType) -> WorkflowTypeLifecycleRead:
    raw = workflow_type.lifecycle_json if isinstance(workflow_type.lifecycle_json, dict) else {}
    states = raw.get("states") if isinstance(raw.get("states"), list) else []
    transitions = raw.get("transitions") if isinstance(raw.get("transitions"), list) else []
    return WorkflowTypeLifecycleRead(
        state_path_kind=str(raw.get("state_path_kind") or "operation").strip().lower() or "operation",
        execution_modes=[str(value).strip() for value in raw.get("execution_modes", []) if str(value).strip()],
        conditional_paths=[str(value).strip() for value in raw.get("conditional_paths", []) if str(value).strip()],
        states=[
            WorkflowTypeLifecycleStateRead(
                key=str(state.get("key") or "").strip(),
                label=str(state.get("label") or "").strip(),
                terminal=bool(state.get("terminal")),
                waits_for_input=bool(state.get("waits_for_input")),
            )
            for state in states
            if isinstance(state, dict) and str(state.get("key") or "").strip() and str(state.get("label") or "").strip()
        ],
        transitions=[
            WorkflowTypeLifecycleTransitionRead(
                **{
                    "from": str(transition.get("from") or "").strip(),
                    "to_state": str(transition.get("to_state") or transition.get("to") or "").strip(),
                    "label": str(transition.get("label") or "").strip(),
                }
            )
            for transition in transitions
            if isinstance(transition, dict)
            and str(transition.get("from") or "").strip()
            and str(transition.get("to_state") or transition.get("to") or "").strip()
            and str(transition.get("label") or "").strip()
        ],
    )


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


def workflow_operation_reads(
    *,
    session,
    workflow: WorkflowExecution,
    operations: list[WorkflowOperation],
    operation_attempts: dict[str, list[WorkflowOperationAttempt]],
    operation_events: dict[str, list[AuditEvent]],
) -> tuple[WorkflowTypeRead, list[WorkflowOperationRead]]:
    workflow_type = get_workflow_type(session, workflow_type_key=workflow.workflow_type_key)
    definitions = list_workflow_type_operations(session, workflow_type_key=workflow_type.workflow_type_key)
    status_by_type = operation_status_by_type(operations=operations)

    def operation_retry_state(
        *,
        retry_policy_config: WorkflowRetryPolicyRead,
        current: WorkflowOperation | None,
    ) -> tuple[bool, str | None]:
        if current is None:
            return False, "Operation has not started yet."
        attempts = operation_attempts.get(current.operation_id, [])
        latest_attempt = attempts[-1] if attempts else None
        if latest_attempt is None:
            return False, "Operation has no attempt history to retry."
        if not retry_policy_config.manual_retry_enabled:
            return False, "Manual retry is disabled by the workflow type."
        if str(latest_attempt.status or "").strip().lower() not in {"failed", "retrying"}:
            return False, "Latest attempt is not in a failed state."
        return True, None

    def default_operation_status(definition_operation_type: str) -> str:
        if definition_operation_type == "run_attempt_execution":
            return str(workflow.status or "").strip() or "pending"
        return "pending"

    workflow_retry_policy = workflow_retry_policy_read(raw_config=workflow_type.retry_policy_config_json)
    type_reads = [
        WorkflowTypeOperationRead(
            operation_type=definition.operation_type,
            label=definition.label,
            description=definition.description,
            completion_required=bool(definition.required),
            status=(
                status_by_type[definition.operation_type].status
                if definition.operation_type in status_by_type
                else default_operation_status(definition.operation_type)
            ),
        )
        for definition in definitions
    ]

    operation_reads: list[WorkflowOperationRead] = []
    defined_operation_types = {definition.operation_type for definition in definitions}
    for definition in definitions:
        current = status_by_type.get(definition.operation_type)
        can_retry, retry_unavailable_reason = operation_retry_state(
            retry_policy_config=workflow_retry_policy,
            current=current,
        )
        operation_reads.append(
            workflow_operation_to_schema(
                current,
                operation_id=(
                    current.operation_id
                    if current is not None
                    else f"{workflow.workflow_id}:{definition.operation_type}"
                ),
                operation_type=definition.operation_type,
                status=(
                    current.status
                    if current is not None
                    else default_operation_status(definition.operation_type)
                ),
                label=definition.label,
                description=definition.description,
                required=bool(definition.required),
                definition_only=current is None,
                attempts=(operation_attempts.get(current.operation_id, []) if current is not None else []),
                events=(operation_events.get(current.operation_id, []) if current is not None else []),
                can_retry=can_retry,
                retry_unavailable_reason=retry_unavailable_reason,
            )
        )

    undefined_operation_types = sorted(
        {
            str(operation.operation_type or "").strip()
            for operation in operations
            if str(operation.operation_type or "").strip()
            and str(operation.operation_type or "").strip() not in defined_operation_types
        }
    )
    if undefined_operation_types:
        raise ValueError(
            "Workflow execution references undefined operations for "
            f"{workflow.workflow_type_key}: {', '.join(undefined_operation_types)}"
        )

    return (
        WorkflowTypeRead(
            key=workflow_type.workflow_type_key,
            label=workflow_type.label,
            description=workflow_type.description,
            orchestration_backend=workflow_type.orchestration_backend,
            retry_policy=workflow_retry_policy,
            capabilities=workflow_type_capabilities_read(workflow_type=workflow_type),
            lifecycle=workflow_type_lifecycle_read(workflow_type=workflow_type),
            operations=type_reads,
        ),
        operation_reads,
    )


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
    workflow_type: WorkflowType,
    tenant_id: str | None,
) -> WorkflowTypeDetailRead:  # noqa: ANN001
    definition_reads = [
        WorkflowTypeOperationRead(
            operation_type=definition.operation_type,
            label=definition.label,
            description=definition.description,
            completion_required=bool(definition.required),
            status=None,
        )
        for definition in list_workflow_type_operations(session, workflow_type_key=workflow_type.workflow_type_key)
    ]
    type_read = WorkflowTypeRead(
        key=workflow_type.workflow_type_key,
        label=workflow_type.label,
        description=workflow_type.description,
        orchestration_backend=workflow_type.orchestration_backend,
        retry_policy=workflow_retry_policy_read(raw_config=workflow_type.retry_policy_config_json),
        capabilities=workflow_type_capabilities_read(workflow_type=workflow_type),
        lifecycle=workflow_type_lifecycle_read(workflow_type=workflow_type),
        operations=definition_reads,
    )
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
    workflow_types = session.execute(
        select(WorkflowType).order_by(WorkflowType.label.asc(), WorkflowType.workflow_type_key.asc())
    ).scalars().all()
    result: list[WorkflowTypeSummaryRead] = []
    for workflow_type in workflow_types:
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
                operation_count=len(list_workflow_type_operations(session, workflow_type_key=workflow_type.workflow_type_key)),
                execution_count=int(session.execute(count_query).scalar_one()),
                latest_execution_at=session.execute(latest_query).scalar_one(),
            )
        )
    return result
