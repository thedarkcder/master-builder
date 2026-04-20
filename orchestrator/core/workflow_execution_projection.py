from __future__ import annotations

from dataclasses import dataclass
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
from orchestrator.core.workflow_type_catalog import list_workflow_type_operations
from orchestrator.storage.models import WorkflowExecution, WorkflowOperation, WorkflowType


def _now() -> datetime:
    return datetime.now(timezone.utc)


def workflow_execution_id(*, workflow_type_key: str, issue_key: str) -> str:
    return f"{str(workflow_type_key or '').strip()}:{str(issue_key or '').strip().upper()}"


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


def _normalize_issue_description(issue_description: object | None) -> str | None:
    if issue_description is None:
        return None
    if isinstance(issue_description, str):
        return issue_description
    if isinstance(issue_description, (dict, list)):
        return json.dumps(issue_description, sort_keys=True)
    return str(issue_description)


@dataclass
class WorkflowExecutionProjection:
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
        mark_workflow_running(workflow=self.workflow, now=_now())

    def mark_waiting_for_input(self, *, operation_type: str, summary: str) -> None:
        operation = self._operation(operation_type)
        attempt = start_workflow_operation_attempt(self.session, operation=operation)
        now = _now()
        mark_workflow_operation_waiting_for_input(
            self.session,
            operation=operation,
            attempt=attempt,
            summary=summary,
        )
        mark_workflow_waiting_for_input(workflow=self.workflow, now=now)

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
    ) -> None:
        operation = self._operation(operation_type)
        attempt = start_workflow_operation_attempt(self.session, operation=operation)
        fail_workflow_operation(
            self.session,
            operation=operation,
            attempt=attempt,
            category=category,
            message=message,
        )
        mark_workflow_failed(workflow=self.workflow, message=message, now=_now())

    def mark_completed_if_ready(self) -> None:
        recompute_workflow_status(session=self.session, workflow=self.workflow, now=_now())


def ensure_issue_workflow_execution(
    *,
    session: Session,
    workflow_type: WorkflowType,
    tenant_id: str,
    project_id: str | None,
    issue_key: str,
    issue_summary: str | None,
    issue_description: object | None,
) -> WorkflowExecutionProjection:
    workflow_id = workflow_execution_id(
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
        workflow.issue_summary = issue_summary
        workflow.issue_description = normalized_issue_description
        workflow.orchestration_backend = workflow_type.orchestration_backend
        workflow.dedupe_scope = workflow_type.system_key
        workflow.updated_at = now
    projection = WorkflowExecutionProjection(session=session, workflow=workflow)
    projection.ensure_operations()
    projection.mark_running()
    return projection


def resolve_latest_issue_workflow(
    *,
    session: Session,
    tenant_id: str,
    issue_key: str,
) -> WorkflowExecution | None:
    return session.execute(
        select(WorkflowExecution)
        .where(
            WorkflowExecution.tenant_id == str(tenant_id or "").strip(),
            WorkflowExecution.issue_key == str(issue_key or "").strip().upper(),
        )
        .order_by(desc(WorkflowExecution.created_at))
        .limit(1)
    ).scalar_one_or_none()
