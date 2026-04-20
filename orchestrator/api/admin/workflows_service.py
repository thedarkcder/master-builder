from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy import desc, func, or_, select
from sqlalchemy.exc import IntegrityError

from orchestrator.api.admin.schema_mappers import workflow_operation_to_schema
from orchestrator.api.schemas import (
    WorkflowActionRead,
    WorkflowExecutionPreviewRead,
    WorkflowLinkRead,
    WorkflowOperationRead,
    WorkflowRetryPolicyRead,
    WorkflowStatePathEntryRead,
    WorkflowTypeDetailRead,
    WorkflowTypeLifecycleRead,
    WorkflowTypeLifecycleStateRead,
    WorkflowTypeLifecycleTransitionRead,
    WorkflowTypeOperationRead,
    WorkflowTypeRead,
    WorkflowTypeSummaryRead,
    WorkflowTypeUpdateRequest,
)
from orchestrator.core.config import get_settings
from orchestrator.core.workflow_attempt_factory import build_workflow_execution_for_attempt
from orchestrator.core.workflow_execution_lifecycle import reconcile_execution_with_active_run_state
from orchestrator.core.workflow_runtime import build_workflow_runtime
from orchestrator.core.workflow_operation_executor import (
    execute_workflow_operation_retry,
    supports_workflow_operation_retry,
    workflow_operation_retry_unavailable_reason,
)
from orchestrator.core.jira_links import tenant_jira_issue_url
from orchestrator.core.runtime_requirements import resolve_required_runtime_kinds_for_workflow
from orchestrator.core.workflow_type_catalog import get_workflow_type, list_workflow_type_operations
from orchestrator.core.workflow_type_catalog import update_workflow_type_configuration
from orchestrator.core.worker_capabilities import infer_required_worker_capability
from orchestrator.core.worker.execution_service import build_run_process_kwargs
from orchestrator.core.worker.process_service import process_claimed_run
from orchestrator.core.worker.runtime_factory import build_workflow_runner_for_session
from orchestrator.core.runs import (
    RunBootstrap,
    RunStateTransitionError,
    require_ready_for_agent_enqueue,
    resolve_enqueue_precheck_outcome,
    resolve_pr_url_for_enqueue,
    resolve_required_worker_capability_from_plan,
)
from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot
from orchestrator.core.workflow.transitions import ATTEMPT_ENTRY_MODES, attempt_creation_policy
from orchestrator.storage.models import (
    DecisionCase,
    DecisionEvent,
    FollowupContext,
    Project,
    Run,
    RunHumanInputRequest,
    Tenant,
    WorkflowCheckpoint,
    WorkflowExecution,
    WorkflowOperation,
    WorkflowOperationAttempt,
    WorkflowType,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _reconcile_workflow_status_with_active_attempt(*, session, workflow) -> None:  # noqa: ANN001
    active_run_id = str(getattr(workflow, "active_run_id", "") or "").strip()
    if not active_run_id:
        return
    active_run = session.get(Run, active_run_id)
    if active_run is None:
        return
    reconcile_execution_with_active_run_state(
        workflow=workflow,
        active_run=active_run,
        now=_now(),
    )


def _latest_checkpoint_for_kind(*, session, workflow_id: str, checkpoint_kind: str) -> WorkflowCheckpoint | None:  # noqa: ANN001
    normalized_kind = str(checkpoint_kind or "").strip().lower()
    compatible_kinds = (
        ("pm",)
        if normalized_kind == "pm"
        else ("execution", "orchestrated")
        if normalized_kind == "execution"
        else (normalized_kind,)
    )
    return session.execute(
        select(WorkflowCheckpoint)
        .where(
            WorkflowCheckpoint.workflow_id == workflow_id,
            WorkflowCheckpoint.checkpoint_kind.in_(compatible_kinds),
        )
        .order_by(desc(WorkflowCheckpoint.created_at))
        .limit(1)
    ).scalar_one_or_none()


def _pending_input_request(*, session, workflow_id: str) -> RunHumanInputRequest | None:  # noqa: ANN001
    return session.execute(
        select(RunHumanInputRequest)
        .where(
            RunHumanInputRequest.workflow_id == workflow_id,
            RunHumanInputRequest.status == "pending",
        )
        .order_by(desc(RunHumanInputRequest.created_at))
        .limit(1)
    ).scalar_one_or_none()


def _workflow_checkpoint_kinds(*, session, workflow_id: str) -> list[str]:  # noqa: ANN001
    checkpoint_kinds = session.execute(
        select(WorkflowCheckpoint.checkpoint_kind)
        .where(WorkflowCheckpoint.workflow_id == workflow_id)
        .order_by(desc(WorkflowCheckpoint.created_at))
    ).scalars().all()
    seen: set[str] = set()
    ordered: list[str] = []
    for kind in checkpoint_kinds:
        normalized = str(kind or "").strip().lower()
        if normalized and normalized not in seen:
            ordered.append(normalized)
            seen.add(normalized)
    return ordered


def _active_followup_contexts(*, session, tenant_id: str, issue_key: str) -> list[FollowupContext]:  # noqa: ANN001
    normalized_tenant_id = str(tenant_id or "").strip()
    normalized_issue_key = str(issue_key or "").strip().upper()
    if not normalized_tenant_id or not normalized_issue_key:
        return []
    return session.execute(
        select(FollowupContext)
        .where(
            FollowupContext.tenant_id == normalized_tenant_id,
            FollowupContext.issue_key == normalized_issue_key,
            FollowupContext.status == "active",
        )
        .order_by(desc(FollowupContext.updated_at))
    ).scalars().all()


def _workflow_runs(*, session, workflow_id: str) -> list[Run]:  # noqa: ANN001
    return session.execute(
        select(Run)
        .where(Run.workflow_id == workflow_id)
        .order_by(Run.attempt_number.asc())
    ).scalars().all()


def _workflow_operations(*, session, workflow_id: str) -> list[WorkflowOperation]:  # noqa: ANN001
    return session.execute(
        select(WorkflowOperation)
        .where(WorkflowOperation.workflow_id == workflow_id)
        .order_by(desc(WorkflowOperation.created_at))
    ).scalars().all()


def _workflow_operation_attempts_by_operation(*, session, workflow_id: str) -> dict[str, list[WorkflowOperationAttempt]]:  # noqa: ANN001
    rows = session.execute(
        select(WorkflowOperationAttempt, WorkflowOperation)
        .join(WorkflowOperation, WorkflowOperation.operation_id == WorkflowOperationAttempt.operation_id)
        .where(WorkflowOperation.workflow_id == workflow_id)
        .order_by(WorkflowOperationAttempt.attempt_number.asc())
    ).all()
    grouped: dict[str, list[WorkflowOperationAttempt]] = {}
    for attempt, operation in rows:
        grouped.setdefault(operation.operation_id, []).append(attempt)
    return grouped


def _latest_run_for_workflow(*, session, workflow_id: str) -> Run | None:  # noqa: ANN001
    return session.execute(
        select(Run)
        .where(Run.workflow_id == workflow_id)
        .order_by(desc(Run.attempt_number))
        .limit(1)
    ).scalar_one_or_none()


def _operation_status_by_type(
    *,
    operations: list[WorkflowOperation],
) -> dict[str, WorkflowOperation]:
    return {
        str(operation.operation_type or "").strip(): operation
        for operation in operations
        if str(operation.operation_type or "").strip()
    }


def _workflow_type_operation_types(*, workflow_type: WorkflowTypeRead) -> set[str]:
    return {
        str(definition.operation_type or "").strip()
        for definition in workflow_type.operations
        if str(definition.operation_type or "").strip()
    }


def _workflow_type_capabilities_read(*, workflow_type: WorkflowType) -> dict[str, object]:
    raw = workflow_type.capabilities_json if isinstance(workflow_type.capabilities_json, dict) else {}
    return dict(raw)


def _workflow_type_lifecycle_read(*, workflow_type: WorkflowType) -> WorkflowTypeLifecycleRead:
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


def _workflow_retry_policy_read(*, raw_config: dict | None) -> WorkflowRetryPolicyRead:
    raw = raw_config if isinstance(raw_config, dict) else {}
    return WorkflowRetryPolicyRead(
        manual_retry_enabled=bool(raw.get("manual_retry_enabled", True)),
        max_attempts=int(raw.get("max_attempts") or 1),
        initial_interval_seconds=int(raw.get("initial_interval_seconds") or 0),
        max_interval_seconds=int(raw.get("max_interval_seconds") or 0),
        backoff_coefficient=float(raw.get("backoff_coefficient") or 1.0),
    )


def _workflow_uses_run_state_path(*, workflow_type: WorkflowTypeRead) -> bool:
    return str(workflow_type.lifecycle.state_path_kind or "").strip().lower() == "run"


def _workflow_supports_child_issue_links(*, workflow_type: WorkflowTypeRead) -> bool:
    return bool(workflow_type.capabilities.get("child_issue_links"))


def _workflow_operation_reads(
    *,
    session,
    workflow: WorkflowExecution,
    operations: list[WorkflowOperation],
    operation_attempts: dict[str, list[WorkflowOperationAttempt]],
) -> tuple[WorkflowTypeRead, list[WorkflowOperationRead]]:
    workflow_type = get_workflow_type(session, workflow_type_key=workflow.workflow_type_key)
    definitions = list_workflow_type_operations(session, workflow_type_key=workflow_type.workflow_type_key)
    status_by_type = _operation_status_by_type(operations=operations)

    def _operation_retry_state(
        *,
        retry_policy_config: WorkflowRetryPolicyRead,
        operation_type: str,
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
        if not bool(latest_attempt.retryable):
            return False, "Latest attempt is not marked retryable."
        if not supports_workflow_operation_retry(operation_type=operation_type):
            return False, workflow_operation_retry_unavailable_reason(operation_type=operation_type)
        return True, None

    def _default_operation_status(definition_operation_type: str) -> str:
        if definition_operation_type == "run_attempt_execution":
            return str(workflow.status or "").strip() or "pending"
        return "pending"

    workflow_retry_policy = _workflow_retry_policy_read(raw_config=workflow_type.retry_policy_config_json)
    type_reads = [
        WorkflowTypeOperationRead(
            operation_type=definition.operation_type,
            label=definition.label,
            description=definition.description,
            completion_required=bool(definition.required),
            status=(
                status_by_type[definition.operation_type].status
                if definition.operation_type in status_by_type
                else _default_operation_status(definition.operation_type)
            ),
        )
        for definition in definitions
    ]

    operation_reads: list[WorkflowOperationRead] = []
    defined_operation_types = {definition.operation_type for definition in definitions}
    for definition in definitions:
        current = status_by_type.get(definition.operation_type)
        can_retry, retry_unavailable_reason = _operation_retry_state(
            retry_policy_config=workflow_retry_policy,
            operation_type=definition.operation_type,
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
                    else _default_operation_status(definition.operation_type)
                ),
                label=definition.label,
                description=definition.description,
                required=bool(definition.required),
                definition_only=current is None,
                attempts=(operation_attempts.get(current.operation_id, []) if current is not None else []),
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
            capabilities=_workflow_type_capabilities_read(workflow_type=workflow_type),
            lifecycle=_workflow_type_lifecycle_read(workflow_type=workflow_type),
            operations=type_reads,
        ),
        operation_reads,
    )


def _run_stage_path(*, latest_run: Run | None) -> list[WorkflowStatePathEntryRead]:
    if latest_run is None or not isinstance(latest_run.plan, dict):
        return []
    snapshot = ExecutionSnapshot.require(latest_run.plan, allow_empty=True)
    stages = snapshot.dump().get("stages", {})
    ordered_entries: list[WorkflowStatePathEntryRead] = []
    label_map = {
        "pm": "PM",
        "dev": "Dev",
        "test": "Test",
        "review": "Review",
    }
    for key in ("pm", "dev", "test", "review"):
        payload = stages.get(key, {})
        if not isinstance(payload, dict):
            continue
        status = str(payload.get("status") or "").strip().lower()
        if not status:
            continue
        ordered_entries.append(
            WorkflowStatePathEntryRead(
                key=key,
                label=label_map.get(key, key.replace("_", " ").title()),
                status=status,
                recorded_at=payload.get("completed_at"),
                detail=str(payload.get("summary") or "").strip() or None,
            )
        )
    return ordered_entries


def _operation_path(
    *,
    workflow_type: WorkflowTypeRead,
    operations: list[WorkflowOperation],
) -> list[WorkflowStatePathEntryRead]:
    status_by_type = _operation_status_by_type(operations=operations)
    entries: list[WorkflowStatePathEntryRead] = []
    for definition in workflow_type.operations:
        current = status_by_type.get(definition.operation_type)
        status = str(
            current.status
            if current is not None
            else ("pending" if definition.completion_required else "not_started")
        ).strip()
        entries.append(
            WorkflowStatePathEntryRead(
                key=definition.operation_type,
                label=definition.label,
                status=status,
                recorded_at=(current.finished_at or current.started_at or current.created_at) if current is not None else None,
                detail=(current.summary if current is not None else definition.description),
            )
        )
    return entries


def _step_buckets(*, state_path: list[WorkflowStatePathEntryRead]) -> tuple[list[str], list[str], list[str], list[str]]:
    completed: list[str] = []
    failed: list[str] = []
    pending: list[str] = []
    retrying: list[str] = []
    for entry in state_path:
        normalized = str(entry.status or "").strip().lower()
        if normalized in {"completed", "succeeded"}:
            completed.append(entry.label)
        elif normalized == "failed":
            failed.append(entry.label)
        elif normalized in {"retrying"}:
            retrying.append(entry.label)
        elif normalized in {"running", "queued", "waiting_for_input", "pending", "not_started"}:
            pending.append(entry.label)
    return completed, failed, pending, retrying


def _waiting_on(
    *,
    pending_request: RunHumanInputRequest | None,
    operations: list[WorkflowOperation],
) -> str | None:
    if pending_request is not None:
        return "human_input"
    if any(str(operation.status or "").strip().lower() == "retrying" for operation in operations):
        return "retry_backoff"
    return None


def _next_step(
    *,
    waiting_on: str | None,
    state_path: list[WorkflowStatePathEntryRead],
    workflow: WorkflowExecution,
) -> str | None:
    if waiting_on == "human_input":
        return "Await human input"
    if waiting_on == "retry_backoff":
        return "Retry failed operation"
    if str(workflow.status or "").strip().lower() in {"completed", "succeeded"}:
        return "Completed"
    for entry in state_path:
        normalized = str(entry.status or "").strip().lower()
        if normalized in {"pending", "not_started", "queued", "running"}:
            return entry.label
    return None


def _branch_sets(*, runs: list[Run]) -> tuple[list[str], list[str]]:
    taken = sorted(
        {
            str(run.entry_mode or "").strip()
            for run in runs
            if str(run.entry_mode or "").strip()
        }
    )
    available = [mode for mode in ATTEMPT_ENTRY_MODES]
    return taken, available


def _available_actions(
    *,
    workflow: WorkflowExecution,
    workflow_runs: list[Run],
    checkpoint_kinds: list[str],
) -> list[WorkflowActionRead]:
    actions: list[WorkflowActionRead] = []
    normalized_checkpoint_kinds = {str(kind or "").strip().lower() for kind in checkpoint_kinds if str(kind or "").strip()}
    for mode in ATTEMPT_ENTRY_MODES:
        policy = attempt_creation_policy(workflow_status=workflow.status, mode=mode)
        if not policy.allowed:
            continue
        checkpoint_kind = None
        detail = None
        if mode in {"restart", "resume"}:
            if "pm" in normalized_checkpoint_kinds:
                checkpoint_kind = "pm"
            elif normalized_checkpoint_kinds.intersection({"execution", "orchestrated"}):
                checkpoint_kind = "execution"
            else:
                continue
            detail = "Resume from the latest compatible checkpoint." if mode == "resume" else "Create a new execution from the latest checkpoint."
        else:
            detail = "Start a fresh execution from the workflow issue context."
        actions.append(
            WorkflowActionRead(
                action_key=f"workflow:{mode}",
                label=f"{mode.replace('_', ' ').title()} execution",
                mode=mode,
                checkpoint_kind=checkpoint_kind,
                detail=detail,
            )
        )
    return actions


def _workflow_links(
    *,
    session,
    workflow: WorkflowExecution,
    workflow_type: WorkflowTypeRead,
    tenant: Tenant | None,
    project: Project | None,
    followup_contexts: list[FollowupContext],
    workflow_runs: list[Run],
    integration_router=None,
) -> list[WorkflowLinkRead]:  # noqa: ANN001
    links: list[WorkflowLinkRead] = []
    jira_url = tenant_jira_issue_url(session=session, tenant=tenant, issue_key=workflow.issue_key) if tenant is not None else None
    if jira_url:
        links.append(
            WorkflowLinkRead(
                kind="jira_issue",
                label=f"Jira issue {workflow.issue_key}",
                ref=workflow.issue_key,
                url=jira_url,
                status=workflow.status,
            )
        )
    if (
        tenant is not None
        and project is not None
        and _workflow_supports_child_issue_links(workflow_type=workflow_type)
        and integration_router is not None
        and str(project.jira_project_key or "").strip()
    ):
        try:
            jira_adapter = integration_router.jira(
                session=session,
                tenant=tenant,
                settings=get_settings(),
            )
            child_previews = jira_adapter.list_child_issue_previews(
                project_key=project.jira_project_key,
                parent_issue_key=workflow.issue_key,
            )
        except Exception:  # noqa: BLE001
            child_previews = []
        for child in child_previews:
            child_url = tenant_jira_issue_url(session=session, tenant=tenant, issue_key=child.key)
            links.append(
                WorkflowLinkRead(
                    kind="child_issue",
                    label=child.summary,
                    ref=child.key,
                    url=child_url,
                    status=child.status,
                )
            )
    if workflow.pr_url:
        links.append(
            WorkflowLinkRead(
                kind="pull_request",
                label="Pull request",
                ref=workflow.pr_url,
                url=workflow.pr_url,
            )
        )
    for run in workflow_runs:
        links.append(
            WorkflowLinkRead(
                kind="run",
                label=f"Run {run.run_id}",
                ref=run.run_id,
                url=None,
                status=run.status,
            )
        )
    for followup in followup_contexts:
        channel_ref = str(followup.thread_channel_id or followup.root_message_id or "").strip()
        if not channel_ref:
            continue
        links.append(
            WorkflowLinkRead(
                kind="followup",
                label="Follow-up thread",
                ref=channel_ref,
                status=followup.status,
            )
        )
    return links


def _workflow_execution_preview(*, session, workflow: WorkflowExecution) -> WorkflowExecutionPreviewRead:  # noqa: ANN001
    operations = _workflow_operations(session=session, workflow_id=workflow.workflow_id)
    pending_request = _pending_input_request(session=session, workflow_id=workflow.workflow_id)
    workflow_type, _ = _workflow_operation_reads(
        session=session,
        workflow=workflow,
        operations=operations,
        operation_attempts=_workflow_operation_attempts_by_operation(session=session, workflow_id=workflow.workflow_id),
    )
    latest_run = _latest_run_for_workflow(session=session, workflow_id=workflow.workflow_id)
    state_path = (
        _run_stage_path(latest_run=latest_run)
        if _workflow_uses_run_state_path(workflow_type=workflow_type)
        else _operation_path(workflow_type=workflow_type, operations=operations)
    )
    waiting_on = _waiting_on(pending_request=pending_request, operations=operations)
    return WorkflowExecutionPreviewRead(
        workflow_id=workflow.workflow_id,
        issue_key=workflow.issue_key,
        issue_summary=workflow.issue_summary,
        status=workflow.status,
        waiting_on=waiting_on,
        next_step=_next_step(waiting_on=waiting_on, state_path=state_path, workflow=workflow),
        failure_reason=workflow.last_error,
        created_at=workflow.created_at,
        finished_at=workflow.finished_at,
    )


def _workflow_type_detail(
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
        retry_policy=_workflow_retry_policy_read(raw_config=workflow_type.retry_policy_config_json),
        capabilities=_workflow_type_capabilities_read(workflow_type=workflow_type),
        lifecycle=_workflow_type_lifecycle_read(workflow_type=workflow_type),
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
        _workflow_execution_preview(session=session, workflow=execution)
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


def _workflow_schema(
    *,
    session,
    workflow,
    workflow_to_schema_fn,
    run_to_schema_fn,
    integration_router=None,
):  # noqa: ANN001
    latest_checkpoint = session.get(WorkflowCheckpoint, workflow.latest_checkpoint_id) if workflow.latest_checkpoint_id else None
    checkpoint_kinds = _workflow_checkpoint_kinds(session=session, workflow_id=workflow.workflow_id)
    workflow_runs = _workflow_runs(session=session, workflow_id=workflow.workflow_id)
    pending_request = _pending_input_request(session=session, workflow_id=workflow.workflow_id)
    operations = _workflow_operations(session=session, workflow_id=workflow.workflow_id)
    operation_attempts = _workflow_operation_attempts_by_operation(session=session, workflow_id=workflow.workflow_id)
    workflow_type, operation_reads = _workflow_operation_reads(
        session=session,
        workflow=workflow,
        operations=operations,
        operation_attempts=operation_attempts,
    )
    latest_run = workflow_runs[-1] if workflow_runs else None
    state_path = (
        _run_stage_path(latest_run=latest_run)
        if _workflow_uses_run_state_path(workflow_type=workflow_type)
        else _operation_path(workflow_type=workflow_type, operations=operations)
    )
    completed_steps, failed_steps, pending_steps, retrying_steps = _step_buckets(state_path=state_path)
    waiting_on = _waiting_on(pending_request=pending_request, operations=operations)
    tenant = session.get(Tenant, workflow.tenant_id)
    project = session.get(Project, workflow.project_id) if str(workflow.project_id or "").strip() else None
    followup_contexts = _active_followup_contexts(session=session, tenant_id=workflow.tenant_id, issue_key=workflow.issue_key)
    conditional_branches_taken, conditional_branches_available = _branch_sets(runs=workflow_runs)
    return workflow_to_schema_fn(
        workflow,
        runs=[run_to_schema_fn(run) for run in workflow_runs],
        pending_input_request_id=(pending_request.request_id if pending_request is not None else None),
        latest_checkpoint_kind=latest_checkpoint.checkpoint_kind if latest_checkpoint is not None else None,
        workflow_type=workflow_type,
        current_state=str(workflow.status or "").strip() or "unknown",
        waiting_on=waiting_on,
        next_step=_next_step(waiting_on=waiting_on, state_path=state_path, workflow=workflow),
        state_path=state_path,
        completed_steps=completed_steps,
        failed_steps=failed_steps,
        pending_steps=pending_steps,
        retrying_steps=retrying_steps,
        conditional_branches_taken=conditional_branches_taken,
        conditional_branches_available=conditional_branches_available,
        available_actions=_available_actions(
            workflow=workflow,
            workflow_runs=workflow_runs,
            checkpoint_kinds=checkpoint_kinds,
        ),
        links=_workflow_links(
            session=session,
            workflow=workflow,
            workflow_type=workflow_type,
            tenant=tenant,
            project=project,
            followup_contexts=followup_contexts,
            workflow_runs=workflow_runs,
            integration_router=integration_router,
        ),
        operations=operation_reads,
    )


def _resolve_project(*, session, workflow, selected_checkpoint) -> Project | None:  # noqa: ANN001
    project_id = str(workflow.project_id or "").strip() or None
    if project_id:
        project = session.get(Project, project_id)
        if project is not None:
            return project
    if selected_checkpoint.run_id:
        parent_run = session.get(Run, selected_checkpoint.run_id)
        parent_project_id = str(getattr(parent_run, "project_id", "") or "").strip() or None
        if parent_project_id:
            return session.get(Project, parent_project_id)
    return None


def _resolve_project_for_fresh_start(*, session, workflow, source_run) -> Project | None:  # noqa: ANN001
    project_id = str(workflow.project_id or "").strip() or None
    if project_id:
        project = session.get(Project, project_id)
        if project is not None:
            return project
    source_project_id = str(getattr(source_run, "project_id", "") or "").strip() or None
    if source_project_id:
        return session.get(Project, source_project_id)
    return None


def _fresh_start_plan(*, source_run: Run | None) -> dict[str, object] | None:
    if source_run is None or source_run.plan is None:
        return None
    source_snapshot = ExecutionSnapshot.require(source_run.plan, allow_empty=False)
    next_snapshot = ExecutionSnapshot.empty(trigger_context=source_snapshot.context.trigger_context)
    precheck_outcome = (
        str(getattr(source_run, "pre_check_outcome", "") or "").strip()
        or str(source_snapshot.context.execution_context.get("pre_check_outcome") or "").strip()
    )
    if precheck_outcome:
        next_snapshot.context.execution_context["pre_check_outcome"] = precheck_outcome
    return next_snapshot.dump()


def _checkpoint_resume_plan(*, checkpoint: WorkflowCheckpoint) -> dict[str, object]:
    return ExecutionSnapshot.require(
        checkpoint.payload_json,
        allow_empty=False,
    ).dump()


def _resolve_precheck_outcome_for_admin_attempt(*, source_run: Run | None, plan: object | None) -> str | None:
    source_precheck = None
    if source_run is not None:
        persisted = str(getattr(source_run, "pre_check_outcome", "") or "").strip()
        if persisted:
            source_precheck = persisted
    return resolve_enqueue_precheck_outcome(
        source="admin_workflow_attempt",
        precheck_outcome=source_precheck,
        precheck_source_plan=plan,
    )


def _latest_decision_issue_labels_for_workflow(*, session, workflow) -> list[str]:  # noqa: ANN001
    event = session.execute(
        select(DecisionEvent)
        .where(
            DecisionEvent.tenant_id == workflow.tenant_id,
            DecisionEvent.issue_key == workflow.issue_key,
        )
        .order_by(desc(DecisionEvent.created_at))
        .limit(1)
    ).scalar_one_or_none()
    if event is None or not isinstance(event.payload_json, dict):
        return []
    labels = event.payload_json.get("issue_labels", [])
    if not isinstance(labels, list):
        return []
    return [str(label).strip() for label in labels if str(label).strip()]


def _resolve_required_worker_capability_for_admin_attempt(*, session, workflow, source_run: Run | None, plan: object | None) -> str | None:  # noqa: ANN001
    if source_run is not None:
        persisted = str(getattr(source_run, "required_worker_capability", "") or "").strip()
        if persisted:
            return persisted
    plan_capability = resolve_required_worker_capability_from_plan(plan)
    if plan_capability:
        return plan_capability
    case = session.execute(
        select(DecisionCase)
        .where(
            DecisionCase.tenant_id == workflow.tenant_id,
            DecisionCase.issue_key == workflow.issue_key,
        )
        .limit(1)
    ).scalar_one_or_none()
    case_capability = str(getattr(case, "required_worker_capability", "") or "").strip()
    if case_capability:
        return case_capability
    inferred = infer_required_worker_capability(
        issue_summary=workflow.issue_summary,
        issue_description=workflow.issue_description,
        issue_labels=_latest_decision_issue_labels_for_workflow(session=session, workflow=workflow),
        tenant_id=workflow.tenant_id,
        project_id=workflow.project_id,
        issue_key=workflow.issue_key,
    )
    return str(inferred or "").strip() or None


def _resolve_pr_url_for_admin_attempt(
    *,
    workflow: WorkflowExecution,
    source_run: Run | None,
    plan: object | None,
) -> str | None:
    source_pr_url = str(getattr(source_run, "pr_url", "") or "").strip() or None
    workflow_pr_url = str(getattr(workflow, "pr_url", "") or "").strip() or None
    return resolve_pr_url_for_enqueue(
        pr_url=source_pr_url or workflow_pr_url,
        pr_url_source_plan=plan,
    )


def _require_ready_for_queue(*, source: str, plan: object | None, precheck_outcome: str | None) -> None:
    try:
        require_ready_for_agent_enqueue(
            source=source,
            precheck_outcome=precheck_outcome,
            precheck_source_plan=plan,
        )
    except RunStateTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


def _cancel_open_input_requests(*, session, workflow_id: str) -> None:  # noqa: ANN001
    open_requests = session.execute(
        select(RunHumanInputRequest).where(
            RunHumanInputRequest.workflow_id == workflow_id,
            RunHumanInputRequest.status.in_(("pending", "answered")),
        )
    ).scalars().all()
    for request in open_requests:
        request.status = "cancelled"


def _next_attempt_number(*, session, workflow_id: str) -> int:  # noqa: ANN001
    existing_runs = _workflow_runs(session=session, workflow_id=workflow_id)
    return (max((run.attempt_number for run in existing_runs), default=0) or 0) + 1


def _is_active_scope_unique_violation(error: IntegrityError) -> bool:
    message = str(error).lower()
    if "uq_workflow_executions_active_scope" in message:
        return True
    return (
        "workflow_executions.tenant_id" in message
        and "workflow_executions.issue_key" in message
        and "workflow_executions.dedupe_scope" in message
    )


def list_workflow_types(
    *,
    session,
    tenant_id: str | None,
):  # noqa: ANN001
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


def get_workflow_type_detail(
    *,
    session,
    workflow_type_key: str,
    tenant_id: str | None,
):  # noqa: ANN001
    workflow_type = get_workflow_type(session, workflow_type_key=workflow_type_key)
    return _workflow_type_detail(session=session, workflow_type=workflow_type, tenant_id=tenant_id)


def update_workflow_type_detail(
    *,
    session,
    workflow_type_key: str,
    tenant_id: str | None,
    payload: WorkflowTypeUpdateRequest,
):  # noqa: ANN001
    del tenant_id
    try:
        workflow_type = update_workflow_type_configuration(
            session,
            workflow_type_key=workflow_type_key,
            orchestration_backend=payload.orchestration_backend,
            retry_policy=payload.retry_policy.model_dump(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    session.commit()
    session.refresh(workflow_type)
    return _workflow_type_detail(session=session, workflow_type=workflow_type, tenant_id=None)


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
    query = select(WorkflowExecution)
    if tenant_id:
        query = query.where(WorkflowExecution.tenant_id == tenant_id)
    if project_id:
        query = query.where(WorkflowExecution.project_id == project_id)
    if status_filter:
        query = query.where(WorkflowExecution.status == status_filter)
    normalized_issue = str(issue_query or "").strip()
    if normalized_issue:
        like_value = f"%{normalized_issue}%"
        query = query.where(
            or_(
                WorkflowExecution.issue_key.ilike(like_value),
                WorkflowExecution.issue_summary.ilike(like_value),
            )
        )
    query = query.order_by(desc(WorkflowExecution.created_at)).limit(limit).offset(offset)
    return [
        _workflow_schema(
            session=session,
            workflow=workflow,
            workflow_to_schema_fn=workflow_to_schema_fn,
            run_to_schema_fn=run_to_schema_fn,
        )
        for workflow in session.execute(query).scalars().all()
    ]


def get_workflow(
    *,
    session,
    workflow_id: str,
    workflow_to_schema_fn,
    run_to_schema_fn,
    integration_router=None,
):  # noqa: ANN001
    workflow = session.get(WorkflowExecution, workflow_id)
    if workflow is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found")
    return _workflow_schema(
        session=session,
        workflow=workflow,
        workflow_to_schema_fn=workflow_to_schema_fn,
        run_to_schema_fn=run_to_schema_fn,
        integration_router=integration_router,
    )


def create_workflow_attempt(
    *,
    session,
    workflow_id: str,
    mode: str,
    checkpoint_kind: str | None,
    tenant_model,
    run_to_schema_fn,
):  # noqa: ANN001
    workflow = session.get(WorkflowExecution, workflow_id)
    if workflow is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found")

    _reconcile_workflow_status_with_active_attempt(session=session, workflow=workflow)
    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode not in ATTEMPT_ENTRY_MODES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid attempt mode")
    creation_policy = attempt_creation_policy(workflow_status=workflow.status, mode=normalized_mode)
    if not creation_policy.allowed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Workflow already has an active attempt",
        )
    selected_checkpoint = None
    source_run = _latest_run_for_workflow(session=session, workflow_id=workflow_id)
    if normalized_mode != "fresh":
        selected_checkpoint = _latest_checkpoint_for_kind(
            session=session,
            workflow_id=workflow_id,
            checkpoint_kind=str(checkpoint_kind or "").strip(),
        )
        if selected_checkpoint is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No checkpoint is available for that kind")

    tenant = session.get(tenant_model, workflow.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found for workflow")

    if normalized_mode == "fresh":
        project = _resolve_project_for_fresh_start(session=session, workflow=workflow, source_run=source_run)
    else:
        project = _resolve_project(session=session, workflow=workflow, selected_checkpoint=selected_checkpoint)
    if project is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No active project mapping found for workflow")
    if bool(getattr(project, "is_archived", False)):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Project {project.project_id} is archived")

    same_workflow = creation_policy.reuse_workflow

    now = _now()
    next_workflow = workflow
    workflow_type = get_workflow_type(session, workflow_type_key=workflow.workflow_type_key)
    orchestration_backend = str(workflow_type.orchestration_backend).strip().lower()
    if not same_workflow:
        next_workflow = build_workflow_execution_for_attempt(
            workflow_id=str(uuid4()),
            workflow_type_key=workflow.workflow_type_key,
            tenant_id=workflow.tenant_id,
            project_id=project.project_id,
            issue_key=workflow.issue_key,
            issue_summary=workflow.issue_summary,
            issue_description=workflow.issue_description,
            repo_url=workflow.repo_url,
            branch=None if normalized_mode == "fresh" else workflow.branch,
            pr_url=None if normalized_mode == "fresh" else workflow.pr_url,
            orchestration_backend=orchestration_backend,
            dedupe_scope=workflow.dedupe_scope,
            status="pending",
            latest_checkpoint_id=selected_checkpoint.checkpoint_id if selected_checkpoint is not None else None,
            source_workflow_id=workflow.workflow_id,
            source_run_id=(
                source_run.run_id
                if normalized_mode == "fresh" and source_run is not None
                else selected_checkpoint.run_id
            ),
            created_at=now,
            updated_at=now,
        )
        session.add(next_workflow)
        session.flush()
    else:
        _cancel_open_input_requests(session=session, workflow_id=workflow.workflow_id)
        next_workflow.latest_checkpoint_id = selected_checkpoint.checkpoint_id if selected_checkpoint is not None else None

    try:
        next_run_plan = (
            _fresh_start_plan(source_run=source_run)
            if normalized_mode == "fresh"
            else _checkpoint_resume_plan(checkpoint=selected_checkpoint)
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Selected run/checkpoint has an unsupported execution snapshot shape",
        ) from exc
    next_run_precheck_outcome = _resolve_precheck_outcome_for_admin_attempt(
        source_run=source_run,
        plan=next_run_plan,
    )
    next_run_required_worker_capability = _resolve_required_worker_capability_for_admin_attempt(
        session=session,
        workflow=next_workflow,
        source_run=source_run,
        plan=next_run_plan,
    )
    next_run_required_runtime_kinds = resolve_required_runtime_kinds_for_workflow(
        session=session,
        settings=get_settings(),
        tenant_id=next_workflow.tenant_id,
        project_id=project.project_id,
    )
    next_run_pr_url = _resolve_pr_url_for_admin_attempt(
        workflow=next_workflow,
        source_run=source_run,
        plan=next_run_plan,
    )
    next_workflow.pr_url = next_run_pr_url

    runtime = build_workflow_runtime(
        session=session,
        settings=get_settings(),
        process_claimed_run_fn=None,
        build_runner_fn=None,
        runtime_kwargs_fn=None,
    )
    try:
        enqueue_result = runtime.create_attempt(
            workflow_id=next_workflow.workflow_id,
            bootstrap=RunBootstrap(
                workflow_id=next_workflow.workflow_id,
                parent_run_id=(
                    source_run.run_id
                    if normalized_mode == "fresh" and source_run is not None
                    else selected_checkpoint.run_id if selected_checkpoint is not None else None
                ),
                entry_mode=normalized_mode,
                entry_stage="orchestrated" if normalized_mode == "fresh" else selected_checkpoint.stage,
                entry_checkpoint_id=None if normalized_mode == "fresh" else selected_checkpoint.checkpoint_id,
                plan=next_run_plan,
                branch=next_workflow.branch,
                pr_url=next_run_pr_url,
                precheck_outcome=next_run_precheck_outcome,
                required_worker_capability=next_run_required_worker_capability,
                required_runtime_kinds_json=next_run_required_runtime_kinds,
            ),
            commit=False,
        )
    except IntegrityError as error:
        session.rollback()
        if _is_active_scope_unique_violation(error):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "A queued or in-progress workflow already exists for this issue "
                    "and dedupe scope. Resume the active workflow instead."
                ),
            ) from error
        raise
    except RunStateTransitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    if not enqueue_result.enqueued:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Workflow already has an active attempt",
        )
    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        if _is_active_scope_unique_violation(error):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "A queued or in-progress workflow already exists for this issue "
                    "and dedupe scope. Resume the active workflow instead."
                ),
            ) from error
        raise
    session.refresh(enqueue_result.run)
    return run_to_schema_fn(enqueue_result.run)


def retry_workflow_operation(
    *,
    session,
    workflow_id: str,
    operation_id: str,
    workflow_to_schema_fn,
    run_to_schema_fn,
    integration_router,
    build_runtime_for_selector_fn,
    seed_issues_with_runtime_fn,
):  # noqa: ANN001
    workflow = session.get(WorkflowExecution, workflow_id)
    if workflow is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found")

    operation = session.get(WorkflowOperation, operation_id)
    if operation is None or operation.workflow_id != workflow.workflow_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow operation not found")

    attempts_by_operation = _workflow_operation_attempts_by_operation(session=session, workflow_id=workflow.workflow_id)
    latest_attempt = (attempts_by_operation.get(operation.operation_id) or [None])[-1]
    if latest_attempt is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Workflow operation has no attempt history to retry")
    if not bool(latest_attempt.retryable):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Workflow operation is not retryable")
    if not supports_workflow_operation_retry(operation_type=operation.operation_type):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=workflow_operation_retry_unavailable_reason(operation_type=operation.operation_type)
            or "Workflow operation does not support retry",
        )

    settings = get_settings()
    runtime = build_workflow_runtime(
        session=session,
        settings=settings,
        process_claimed_run_fn=process_claimed_run,
        build_runner_fn=build_workflow_runner_for_session,
        runtime_kwargs_fn=build_run_process_kwargs,
        retry_workflow_operation_fn=lambda **kwargs: execute_workflow_operation_retry(
            **kwargs,
            integration_router=integration_router,
            build_runtime_for_selector_fn=build_runtime_for_selector_fn,
            seed_issues_with_runtime_fn=seed_issues_with_runtime_fn,
        ),
    )
    runtime.retry_operation(
        workflow=workflow,
        operation=operation,
    )
    session.commit()
    return _workflow_schema(
        session=session,
        workflow=workflow,
        workflow_to_schema_fn=workflow_to_schema_fn,
        run_to_schema_fn=run_to_schema_fn,
        integration_router=integration_router,
    )
