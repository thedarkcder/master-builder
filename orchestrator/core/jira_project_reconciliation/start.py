from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from orchestrator.core.jira_project_reconciliation.dependencies import (
    JiraProjectReconciliationHandlerDeps,
)
from orchestrator.core.jira_project_reconciliation.handlers import (
    JiraProjectReconciliationAdvanceHandler,
)
from orchestrator.core.jira_project_reconciliation.retry import (
    JiraProjectReconciliationOperationRetryHandler,
)
from orchestrator.core.workflow.execution_projection import (
    WorkflowExecutionReference,
    WorkflowSourceReference,
)
from orchestrator.core.workflow.handler_registry import build_workflow_handler_registry
from orchestrator.core.workflow.runtime import (
    WorkflowAdvanceRequest,
    WorkflowTrigger,
    build_workflow_runtime,
)
from orchestrator.storage.models import (
    Project,
    Tenant,
    WorkflowExecution,
    WorkflowOperation,
    WorkflowOperationAttempt,
)


@dataclass(frozen=True)
class WorkflowExecutionStartResult:
    execution_id: str
    workflow_id: str
    workflow_type_key: str
    status: str
    started_attempt_id: str | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _request_id(*, tenant: Tenant, project: Project, trigger_event: str) -> str:
    return (
        f"jira-project-reconciliation:{tenant.tenant_id}:{project.project_id}:{trigger_event}:"
        f"{_now().isoformat(timespec='seconds')}"
    )


def _latest_attempt_id(*, session: Session, workflow_id: str) -> str | None:
    attempt = session.execute(
        select(WorkflowOperationAttempt)
        .join(
            WorkflowOperation,
            WorkflowOperation.operation_id == WorkflowOperationAttempt.operation_id,
        )
        .where(WorkflowOperation.workflow_id == workflow_id)
        .order_by(desc(WorkflowOperationAttempt.created_at))
        .limit(1)
    ).scalar_one_or_none()
    if attempt is None:
        return None
    return attempt.attempt_id


def start_jira_project_reconciliation(
    *,
    session: Session,
    settings,  # noqa: ANN001
    tenant: Tenant,
    project: Project,
    max_items: int = 1000,
    trigger_event: str,
    gateway_factory=None,
) -> WorkflowExecutionStartResult:
    if tenant is None:
        raise ValueError("Jira project reconciliation start requires tenant")
    if project is None:
        raise ValueError("Jira project reconciliation start requires project")
    if project.tenant_id != tenant.tenant_id:
        raise ValueError(
            "Jira project reconciliation project must belong to the supplied tenant"
        )
    jira_project_key = (
        str(getattr(project, "jira_project_key", "") or "").strip().upper()
    )
    if not jira_project_key:
        raise ValueError("Jira project reconciliation start requires project Jira key")

    handler_registry = build_workflow_handler_registry(
        advance_handlers={
            "jira_project_reconciliation": JiraProjectReconciliationAdvanceHandler(
                deps=JiraProjectReconciliationHandlerDeps(
                    gateway_factory=gateway_factory
                ),
            ),
        },
        operation_retry_handlers={
            "jira_project_reconciliation": JiraProjectReconciliationOperationRetryHandler(
                deps=JiraProjectReconciliationHandlerDeps(
                    gateway_factory=gateway_factory
                ),
            ),
        },
    )
    runtime = build_workflow_runtime(
        session=session,
        settings=settings,
        process_claimed_run_fn=None,
        build_runner_fn=None,
        runtime_kwargs_fn=None,
        resolve_advance_handler_fn=handler_registry.resolve_advance_handler,
        workflow_handler_registry=handler_registry,
    )
    result = runtime.advance(
        request=WorkflowAdvanceRequest(
            workflow_handler_key="jira_project_reconciliation",
            tenant_id=tenant.tenant_id,
            tenant=tenant,
            project_id=project.project_id,
            execution=WorkflowExecutionReference(
                key=project.project_id,
                source=WorkflowSourceReference(
                    source_system="jira_project",
                    source_ref=jira_project_key,
                    display_name=f"Jira reconciliation {jira_project_key}",
                    description=f"Reconcile Jira project {jira_project_key} into Master Builder parent planning workflows.",
                ),
            ),
            payload={
                "request_id": _request_id(
                    tenant=tenant, project=project, trigger_event=trigger_event
                ),
                "max_items": max(1, int(max_items)),
            },
            trigger=WorkflowTrigger(event=trigger_event),
        )
    )
    workflow = session.execute(
        select(WorkflowExecution)
        .where(
            WorkflowExecution.workflow_type_key == "jira_project_reconciliation",
            WorkflowExecution.project_id == project.project_id,
        )
        .order_by(desc(WorkflowExecution.created_at))
        .limit(1)
    ).scalar_one_or_none()
    if workflow is None:
        raise RuntimeError(
            "Jira project reconciliation did not create a durable workflow execution"
        )
    _ = result
    return WorkflowExecutionStartResult(
        execution_id=workflow.execution_id,
        workflow_id=workflow.workflow_id,
        workflow_type_key=workflow.workflow_type_key,
        status=workflow.status,
        started_attempt_id=_latest_attempt_id(
            session=session, workflow_id=workflow.workflow_id
        ),
    )
