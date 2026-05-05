from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from orchestrator.core.workflow.execution_status import (
    mark_workflow_failed,
    mark_workflow_running,
    mark_workflow_waiting_for_input,
    recompute_workflow_status,
)
from orchestrator.core.workflow.operation_service import (
    complete_workflow_operation,
    fail_workflow_operation,
    mark_workflow_operation_waiting_for_input,
    OPERATION_STATUS_PENDING,
    OPERATION_STATUS_RUNNING,
    start_workflow_operation_attempt,
    upsert_workflow_operation,
)
from orchestrator.core.workflow.definition import WorkflowDefinition
from orchestrator.storage.models import WorkflowExecution, WorkflowOperation, WorkflowOperationAttempt


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class WorkflowSourceReference:
    source_system: str
    source_ref: str
    external_id: str | None = None
    display_name: str | None = None
    description: object | None = None
    attributes: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.source_system or "").strip():
            raise ValueError("Workflow source reference source_system is required")
        if not str(self.source_ref or "").strip():
            raise ValueError("Workflow source reference source_ref is required")


@dataclass(frozen=True)
class WorkflowExecutionReference:
    key: str
    source: WorkflowSourceReference

    def __post_init__(self) -> None:
        if not str(self.key or "").strip():
            raise ValueError("Workflow execution reference key is required")


def workflow_execution_id(*, workflow_type_key: str, execution_key: str) -> str:
    normalized_type = str(workflow_type_key or "").strip()
    normalized_key = str(execution_key or "").strip()
    if not normalized_type:
        raise ValueError("workflow_type_key is required")
    if not normalized_key:
        raise ValueError("execution_key is required")
    return f"{normalized_type}:{normalized_key}"


def classify_external_workflow_failure(*, error: Exception) -> str:
    message = str(error or "").strip()
    lowered = message.lower()
    if "CONTENT_LIMIT_EXCEEDED" in message:
        return "content_limit"
    if "429" in message or "rate limit" in lowered:
        return "rate_limited"
    if "502" in message or "503" in message or "504" in message or "timed out" in lowered:
        return "transient_external_failure"
    return "external_failure"


def _target_system_for_operation(operation_type: str) -> str | None:
    normalized = str(operation_type or "").strip()
    if normalized.startswith("jira_"):
        return "jira"
    if normalized.startswith("discord_"):
        return "discord"
    if normalized.startswith("notification_"):
        return "notification"
    return None


def _normalize_source_description(source_description: object | None) -> str | None:
    if source_description is None:
        return None
    if isinstance(source_description, str):
        return source_description
    if isinstance(source_description, (dict, list)):
        return json.dumps(source_description, sort_keys=True)
    return str(source_description)


def _descendant_operation_types(*, workflow_type: WorkflowDefinition, operation_type: str) -> set[str]:
    normalized = str(operation_type or "").strip()
    descendants: set[str] = set()
    changed = True
    while changed:
        changed = False
        for definition in workflow_type.steps:
            if definition.key == normalized or definition.key in descendants:
                continue
            dependencies = {str(dependency or "").strip() for dependency in definition.after}
            if normalized in dependencies or descendants.intersection(dependencies):
                descendants.add(definition.key)
                changed = True
    return descendants


@dataclass
class WorkflowExecutionProjection:
    session: Session
    workflow: WorkflowExecution
    workflow_type: WorkflowDefinition

    def ensure_operations(self) -> None:
        for definition in self.workflow_type.steps:
            upsert_workflow_operation(
                self.session,
                workflow_id=self.workflow.workflow_id,
                operation_type=definition.key,
                idempotency_key=f"workflow-definition:{definition.key}",
                target_system=_target_system_for_operation(definition.key),
                target_ref=self.workflow.source_ref if _target_system_for_operation(definition.key) else None,
                summary=definition.description,
            )

    def _operation(
        self,
        operation_type: str,
        *,
        run_id: str | None = None,
        idempotency_key: str | None = None,
        target_system: str | None = None,
        target_ref: str | None = None,
        summary: str | None = None,
    ) -> WorkflowOperation:
        self.workflow_type.step(operation_type)
        normalized_idempotency_key = str(idempotency_key or "").strip() or f"workflow-definition:{operation_type}"
        operation = self.session.execute(
            select(WorkflowOperation).where(
                WorkflowOperation.workflow_id == self.workflow.workflow_id,
                WorkflowOperation.idempotency_key == normalized_idempotency_key,
            )
        ).scalar_one_or_none()
        if operation is None:
            default_target_system = _target_system_for_operation(operation_type)
            resolved_target_ref = (
                target_ref
                if target_ref is not None
                else self.workflow.source_ref
                if default_target_system
                else None
            )
            operation = upsert_workflow_operation(
                self.session,
                workflow_id=self.workflow.workflow_id,
                operation_type=operation_type,
                idempotency_key=normalized_idempotency_key,
                run_id=run_id,
                target_system=target_system if target_system is not None else default_target_system,
                target_ref=resolved_target_ref,
                summary=summary,
            )
        else:
            operation.run_id = run_id or operation.run_id
            operation.target_system = target_system or operation.target_system
            operation.target_ref = target_ref or operation.target_ref
            operation.summary = summary or operation.summary
            operation.updated_at = _now()
        return operation

    def mark_running(self) -> None:
        mark_workflow_running(workflow=self.workflow, now=_now())

    def mark_workflow_waiting_for_input(self) -> None:
        mark_workflow_waiting_for_input(workflow=self.workflow, now=_now())

    def start_operation_attempt(
        self,
        *,
        operation_type: str,
        run_id: str | None = None,
        idempotency_key: str | None = None,
        target_system: str | None = None,
        target_ref: str | None = None,
        summary: str | None = None,
    ) -> tuple[WorkflowOperation, WorkflowOperationAttempt]:
        self._invalidate_descendant_operations(operation_type=operation_type)
        operation = self._operation(
            operation_type,
            run_id=run_id,
            idempotency_key=idempotency_key,
            target_system=target_system,
            target_ref=target_ref,
            summary=summary,
        )
        attempt = start_workflow_operation_attempt(self.session, operation=operation)
        self.mark_running()
        self.session.commit()
        return operation, attempt

    def _invalidate_descendant_operations(self, *, operation_type: str) -> None:
        descendant_types = _descendant_operation_types(workflow_type=self.workflow_type, operation_type=operation_type)
        if not descendant_types:
            return
        operations = self.session.execute(
            select(WorkflowOperation).where(
                WorkflowOperation.workflow_id == self.workflow.workflow_id,
                WorkflowOperation.operation_type.in_(descendant_types),
            )
        ).scalars()
        definitions = {definition.key: definition for definition in self.workflow_type.steps}
        now = _now()
        for operation in operations:
            if str(operation.status or "").strip().lower() == OPERATION_STATUS_RUNNING:
                raise RuntimeError(
                    "Cannot start workflow operation "
                    f"{operation_type}; dependent operation {operation.operation_type} is already running."
                )
            operation.status = OPERATION_STATUS_PENDING
            operation.summary = definitions.get(operation.operation_type).description if operation.operation_type in definitions else None
            operation.started_at = None
            operation.finished_at = None
            operation.updated_at = now

    def complete_started_operation(self, *, operation: WorkflowOperation, attempt: WorkflowOperationAttempt, summary: str) -> None:
        complete_workflow_operation(
            self.session,
            operation=operation,
            attempt=attempt,
            summary=summary,
        )
        self.mark_completed_if_ready()
        self.session.commit()

    def fail_started_operation(
        self,
        *,
        operation: WorkflowOperation,
        attempt: WorkflowOperationAttempt,
        category: str,
        message: str,
    ) -> None:
        fail_workflow_operation(
            self.session,
            operation=operation,
            attempt=attempt,
            category=category,
            message=message,
        )
        mark_workflow_failed(workflow=self.workflow, message=message, now=_now())
        self.session.commit()

    def wait_started_operation(
        self,
        *,
        operation: WorkflowOperation,
        attempt: WorkflowOperationAttempt,
        summary: str,
    ) -> None:
        mark_workflow_operation_waiting_for_input(
            self.session,
            operation=operation,
            attempt=attempt,
            summary=summary,
        )
        mark_workflow_waiting_for_input(workflow=self.workflow, now=_now())
        self.session.commit()

    def mark_completed_if_ready(self) -> None:
        recompute_workflow_status(session=self.session, workflow=self.workflow, now=_now())


def ensure_workflow_execution(
    *,
    session: Session,
    workflow_type: WorkflowDefinition,
    tenant_id: str,
    project_id: str | None,
    execution: WorkflowExecutionReference,
    display_name: str | None,
    description: object | None,
) -> WorkflowExecutionProjection:
    workflow_id = workflow_execution_id(
        workflow_type_key=workflow_type.workflow_type_key,
        execution_key=execution.key,
    )
    normalized_description = _normalize_source_description(description)
    source_ref = str(execution.source.source_ref or "").strip()
    source_external_id = str(execution.source.external_id or "").strip() or None
    workflow = session.get(WorkflowExecution, workflow_id)
    if workflow is None and source_external_id is not None:
        workflow = session.execute(
            select(WorkflowExecution)
            .where(
                WorkflowExecution.tenant_id == tenant_id,
                WorkflowExecution.source_system == execution.source.source_system,
                WorkflowExecution.source_external_id == source_external_id,
                WorkflowExecution.dedupe_scope == workflow_type.system_key,
            )
            .order_by(desc(WorkflowExecution.created_at))
            .limit(1)
        ).scalar_one_or_none()
    now = _now()
    if workflow is None:
        workflow = WorkflowExecution(
            workflow_id=workflow_id,
            workflow_type_key=workflow_type.workflow_type_key,
            tenant_id=tenant_id,
            project_id=project_id,
            source_system=execution.source.source_system,
            source_ref=source_ref,
            source_external_id=source_external_id,
            display_name=display_name,
            source_description=normalized_description,
            repo_url=None,
            branch=None,
            pr_url=None,
            orchestration_backend=workflow_type.orchestration_backend,
            dedupe_scope=workflow_type.system_key,
            status="running",
            last_error=None,
            active_run_id=None,
            latest_checkpoint_id=None,
            source_workflow_id=None,
            source_run_id=None,
            created_at=now,
            started_at=now,
            finished_at=None,
            updated_at=now,
        )
        session.add(workflow)
        session.flush()
    else:
        if (
            workflow.source_external_id
            and source_external_id
            and str(workflow.source_external_id or "").strip() != source_external_id
        ):
            raise ValueError("Workflow source external id does not match the supplied source reference")
        workflow.project_id = project_id or workflow.project_id
        workflow.source_system = execution.source.source_system
        workflow.source_ref = source_ref
        workflow.source_external_id = source_external_id or workflow.source_external_id
        workflow.display_name = display_name
        workflow.source_description = normalized_description
        workflow.orchestration_backend = workflow_type.orchestration_backend
        workflow.dedupe_scope = workflow_type.system_key
        workflow.updated_at = now
    projection = WorkflowExecutionProjection(session=session, workflow=workflow, workflow_type=workflow_type)
    projection.ensure_operations()
    return projection


def resolve_latest_workflow_execution_by_source(
    *,
    session: Session,
    tenant_id: str,
    source_system: str,
    source_ref: str,
) -> WorkflowExecution | None:
    if not str(source_system or "").strip():
        raise ValueError("source_system is required")
    return session.execute(
        select(WorkflowExecution)
        .where(
            WorkflowExecution.tenant_id == str(tenant_id or "").strip(),
            WorkflowExecution.source_system == str(source_system or "").strip(),
            WorkflowExecution.source_ref == str(source_ref or "").strip().upper(),
        )
        .order_by(desc(WorkflowExecution.created_at))
        .limit(1)
    ).scalar_one_or_none()
