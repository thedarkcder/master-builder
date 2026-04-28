from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from orchestrator.core.workflow_execution_status import (
    mark_workflow_failed,
    mark_workflow_running,
    mark_workflow_waiting_for_input,
    recompute_workflow_status,
)
from orchestrator.core.workflow_operation_service import (
    complete_workflow_operation,
    fail_workflow_operation,
    mark_workflow_operation_waiting_for_input,
    start_workflow_operation_attempt,
    upsert_workflow_operation,
)
from orchestrator.core.workflow_definition import WorkflowDefinition
from orchestrator.storage.models import WorkflowExecution, WorkflowOperation, WorkflowOperationAttempt


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class WorkflowSourceReference:
    source_system: str
    source_ref: str
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

    def _operation(self, operation_type: str) -> WorkflowOperation:
        self.workflow_type.step(operation_type)
        operation = self.session.execute(
            select(WorkflowOperation).where(
                WorkflowOperation.workflow_id == self.workflow.workflow_id,
                WorkflowOperation.operation_type == operation_type,
            )
        ).scalar_one_or_none()
        if operation is None:
            operation = upsert_workflow_operation(
                self.session,
                workflow_id=self.workflow.workflow_id,
                operation_type=operation_type,
                idempotency_key=f"workflow-definition:{operation_type}",
                target_system=_target_system_for_operation(operation_type),
                target_ref=self.workflow.source_ref if _target_system_for_operation(operation_type) else None,
                summary=None,
            )
        return operation

    def mark_running(self) -> None:
        mark_workflow_running(workflow=self.workflow, now=_now())

    def mark_workflow_waiting_for_input(self) -> None:
        mark_workflow_waiting_for_input(workflow=self.workflow, now=_now())

    def start_operation_attempt(self, *, operation_type: str) -> tuple[WorkflowOperation, WorkflowOperationAttempt]:
        operation = self._operation(operation_type)
        attempt = start_workflow_operation_attempt(self.session, operation=operation)
        return operation, attempt

    def complete_started_operation(self, *, operation: WorkflowOperation, attempt: WorkflowOperationAttempt, summary: str) -> None:
        complete_workflow_operation(
            self.session,
            operation=operation,
            attempt=attempt,
            summary=summary,
        )
        self.mark_completed_if_ready()

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
    workflow = session.get(WorkflowExecution, workflow_id)
    now = _now()
    if workflow is None:
        workflow = WorkflowExecution(
            workflow_id=workflow_id,
            workflow_type_key=workflow_type.workflow_type_key,
            tenant_id=tenant_id,
            project_id=project_id,
            source_system=execution.source.source_system,
            source_ref=source_ref,
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
        workflow.project_id = project_id or workflow.project_id
        workflow.source_system = execution.source.source_system
        workflow.source_ref = source_ref
        workflow.display_name = display_name
        workflow.source_description = normalized_description
        workflow.orchestration_backend = workflow_type.orchestration_backend
        workflow.dedupe_scope = workflow_type.system_key
        workflow.updated_at = now
    projection = WorkflowExecutionProjection(session=session, workflow=workflow, workflow_type=workflow_type)
    projection.ensure_operations()
    projection.mark_running()
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
