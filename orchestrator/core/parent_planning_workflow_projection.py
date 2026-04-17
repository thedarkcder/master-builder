from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.core.workflow_operation_service import (
    OPERATION_STATUS_COMPLETED,
    complete_workflow_operation,
    fail_workflow_operation,
    start_workflow_operation_attempt,
    upsert_workflow_operation,
)
from orchestrator.core.workflow_type_catalog import (
    get_workflow_type_by_system_key,
    list_workflow_type_operations,
)
from orchestrator.storage.models import WorkflowExecution, WorkflowOperation

PARENT_PLANNING_WORKFLOW_SYSTEM_KEY = "parent_planning"
WORKFLOW_DEDUPE_SCOPE_PARENT_PLANNING = "parent_planning"
OPERATION_STATUS_WAITING_FOR_INPUT = "waiting_for_input"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def parent_planning_workflow_id(*, workflow_type_key: str, issue_key: str) -> str:
    return f"{str(workflow_type_key or '').strip()}:{str(issue_key or '').strip().upper()}"


def _target_system_for_operation(operation_type: str) -> str | None:
    normalized = str(operation_type or "").strip()
    if normalized.startswith("jira_"):
        return "jira"
    if normalized.startswith("discord_"):
        return "discord"
    if normalized.startswith("notification_"):
        return "notification"
    return None


def _normalize_issue_description(issue_description: object | None) -> str | None:
    if issue_description is None:
        return None
    if isinstance(issue_description, str):
        return issue_description
    if isinstance(issue_description, (dict, list)):
        return json.dumps(issue_description, sort_keys=True)
    return str(issue_description)


@dataclass
class ParentPlanningWorkflowProjection:
    session: Session
    workflow: WorkflowExecution

    def ensure_operations(self) -> None:
        for definition in list_workflow_type_operations(self.session, workflow_type_key=self.workflow.workflow_type_key):
            upsert_workflow_operation(
                self.session,
                workflow_id=self.workflow.workflow_id,
                operation_type=definition.operation_type,
                idempotency_key=f"workflow-definition:{definition.operation_type}",
                target_system=_target_system_for_operation(definition.operation_type),
                target_ref=self.workflow.issue_key if _target_system_for_operation(definition.operation_type) else None,
                summary=definition.description,
            )

    def _operation(self, operation_type: str) -> WorkflowOperation:
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
                target_ref=self.workflow.issue_key if _target_system_for_operation(operation_type) else None,
                summary=None,
            )
        return operation

    def mark_running(self) -> None:
        self.workflow.status = "running"
        self.workflow.last_error = None
        self.workflow.started_at = self.workflow.started_at or _now()
        self.workflow.finished_at = None
        self.workflow.updated_at = _now()

    def mark_waiting_for_input(self, *, operation_type: str, summary: str) -> None:
        operation = self._operation(operation_type)
        attempt = start_workflow_operation_attempt(self.session, operation=operation)
        now = _now()
        operation.status = OPERATION_STATUS_WAITING_FOR_INPUT
        operation.summary = summary
        operation.finished_at = None
        operation.updated_at = now
        attempt.status = OPERATION_STATUS_WAITING_FOR_INPUT
        attempt.error_category = None
        attempt.error_message = summary
        attempt.retryable = False
        attempt.next_retry_at = None
        attempt.finished_at = now
        self.workflow.status = "waiting_for_input"
        self.workflow.last_error = None
        self.workflow.finished_at = None
        self.workflow.updated_at = now

    def mark_operation_completed(self, *, operation_type: str, summary: str) -> None:
        operation = self._operation(operation_type)
        attempt = start_workflow_operation_attempt(self.session, operation=operation)
        complete_workflow_operation(
            self.session,
            operation=operation,
            attempt=attempt,
            summary=summary,
        )
        self.mark_running()

    def mark_operation_failed(
        self,
        *,
        operation_type: str,
        category: str,
        message: str,
        retryable: bool,
    ) -> None:
        operation = self._operation(operation_type)
        attempt = start_workflow_operation_attempt(self.session, operation=operation)
        fail_workflow_operation(
            self.session,
            operation=operation,
            attempt=attempt,
            category=category,
            message=message,
            retryable=retryable,
        )
        now = _now()
        self.workflow.status = "failed"
        self.workflow.last_error = message
        self.workflow.finished_at = now
        self.workflow.updated_at = now

    def mark_completed_if_ready(self) -> None:
        now = _now()
        operations = self.session.execute(
            select(WorkflowOperation).where(WorkflowOperation.workflow_id == self.workflow.workflow_id)
        ).scalars().all()
        status_by_type = {str(operation.operation_type or "").strip(): str(operation.status or "").strip().lower() for operation in operations}
        required_definitions = [
            definition
            for definition in list_workflow_type_operations(self.session, workflow_type_key=self.workflow.workflow_type_key)
            if bool(definition.required)
        ]
        if required_definitions and all(
            status_by_type.get(definition.operation_type) == OPERATION_STATUS_COMPLETED
            for definition in required_definitions
        ):
            self.workflow.status = "completed"
            self.workflow.last_error = None
            self.workflow.finished_at = now
            self.workflow.updated_at = now


def classify_parent_planning_failure(*, error: Exception) -> tuple[str, bool]:
    message = str(error or "").strip()
    lowered = message.lower()
    if "CONTENT_LIMIT_EXCEEDED" in message:
        return "content_limit", True
    if "429" in message or "rate limit" in lowered:
        return "rate_limited", True
    if "502" in message or "503" in message or "504" in message or "timed out" in lowered:
        return "transient_external_failure", True
    return "external_failure", False


def ensure_parent_planning_workflow(
    *,
    session: Session,
    tenant_id: str,
    project_id: str | None,
    issue_key: str,
    issue_summary: str | None,
    issue_description: object | None,
) -> ParentPlanningWorkflowProjection:
    workflow_type = get_workflow_type_by_system_key(
        session,
        system_key=PARENT_PLANNING_WORKFLOW_SYSTEM_KEY,
    )
    workflow_id = parent_planning_workflow_id(
        workflow_type_key=workflow_type.workflow_type_key,
        issue_key=issue_key,
    )
    normalized_issue_description = _normalize_issue_description(issue_description)
    workflow = session.get(WorkflowExecution, workflow_id)
    now = _now()
    if workflow is None:
        workflow = WorkflowExecution(
            workflow_id=workflow_id,
            workflow_type_key=workflow_type.workflow_type_key,
            tenant_id=tenant_id,
            project_id=project_id,
            issue_key=str(issue_key or "").strip().upper(),
            issue_summary=issue_summary,
            issue_description=normalized_issue_description,
            repo_url=None,
            branch=None,
            pr_url=None,
            orchestration_backend=workflow_type.orchestration_backend,
            dedupe_scope=WORKFLOW_DEDUPE_SCOPE_PARENT_PLANNING,
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
        workflow.issue_summary = issue_summary
        workflow.issue_description = normalized_issue_description
        workflow.orchestration_backend = workflow_type.orchestration_backend
        workflow.updated_at = now
    projection = ParentPlanningWorkflowProjection(session=session, workflow=workflow)
    projection.ensure_operations()
    projection.mark_running()
    return projection
