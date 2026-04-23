from __future__ import annotations

from temporalio import activity
from temporalio.exceptions import ApplicationError

from orchestrator.api.discord.ingress.seed_runtime import seed_issues_with_runtime
from orchestrator.api.discord.seed.issue_service import list_child_issue_previews_for_parent
from orchestrator.api.atlassian_oauth.connection_service import tenant_atlassian_oauth_context
from orchestrator.api.webhooks.contracts import (
    create_jira_comment,
    extract_changed_fields,
    extract_status_transition,
    post_jira_comment,
)
from orchestrator.core.agent_runtime_resolver import build_runtime_for_selector
from orchestrator.core.config import get_settings
from orchestrator.core.workflow_advance import (
    InvalidWorkflowOperationRetryError,
    UnsupportedWorkflowOperationRetryError,
    WorkflowAdvanceRequest,
    execute_workflow_advance,
    execute_workflow_operation_retry,
)
from orchestrator.core.workflow_integration_provider import (
    JiraWorkflowConnectionProvider,
    WorkflowIntegrationAdapterProvider,
)
from orchestrator.core.workflow_integration_router import WorkflowIntegrationRouter
from orchestrator.core.workflow_handler_registry import build_workflow_handler_registry
from orchestrator.core.workflow_type_catalog import get_workflow_type_by_handler_key
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import Tenant, WorkflowExecution, WorkflowOperation
from orchestrator.temporal.payloads import (
    HandlerWorkflowAdvanceResult,
    HandlerWorkflowAdvanceInput,
    WorkflowOperationRetryInput,
    WorkflowOperationRetryResult,
)


def _build_workflow_integration_router() -> WorkflowIntegrationRouter:
    return WorkflowIntegrationRouter(
        adapter_provider=WorkflowIntegrationAdapterProvider(
            jira_provider=JiraWorkflowConnectionProvider(
                oauth_context_resolver=tenant_atlassian_oauth_context,
                list_child_issue_previews_for_parent_fn=list_child_issue_previews_for_parent,
            )
        )
    )


def _workflow_status_payload(*, workflow: WorkflowExecution) -> dict[str, str | None]:
    return {
        "status": str(workflow.status or "").strip(),
        "active_run_id": str(workflow.active_run_id or "").strip() or None,
        "last_error": str(workflow.last_error or "").strip() or None,
    }


@activity.defn(name="process_handler_workflow_advance_activity")
def process_handler_workflow_advance_activity(
    activity_input: HandlerWorkflowAdvanceInput,
) -> HandlerWorkflowAdvanceResult:
    workflow_id = str(activity_input.workflow_id or "").strip()
    payload = activity_input
    settings = get_settings()
    session_factory = create_session_factory()
    with session_factory() as session:
        tenant = session.get(Tenant, str(payload.tenant_id or "").strip())
        if tenant is None:
            raise RuntimeError(f"Workflow advance is missing tenant {payload.tenant_id}")
        workflow_type = get_workflow_type_by_handler_key(
            session,
            handler_key=str(payload.workflow_handler_key or "").strip(),
        )
        request = WorkflowAdvanceRequest(
            workflow_handler_key=payload.workflow_handler_key,
            tenant_id=payload.tenant_id,
            tenant=tenant,
            project_id=payload.project_id,
            issue_key=payload.issue_key,
            issue_summary=payload.issue_summary,
            issue_description=payload.issue_description,
            issue_labels=tuple(payload.issue_labels or ()),
            payload=dict(payload.payload or {}),
            webhook_event=payload.webhook_event,
            comment_command=payload.comment_command,
            comment_command_argument=payload.comment_command_argument,
        )
        handler_registry = build_workflow_handler_registry(
            integration_router=_build_workflow_integration_router(),
            extract_changed_fields_fn=extract_changed_fields,
            extract_status_transition_fn=extract_status_transition,
            build_runtime_for_selector_fn=build_runtime_for_selector,
            seed_issues_with_runtime_fn=seed_issues_with_runtime,
            post_jira_comment_fn=post_jira_comment,
            create_jira_comment_fn=create_jira_comment,
        )
        result = execute_workflow_advance(
            session=session,
            settings=settings,
            workflow_type=workflow_type,
            request=request,
            resolve_advance_handler_fn=handler_registry.resolve_advance_handler,
        )
        session.commit()
        workflow = session.get(WorkflowExecution, workflow_id)
        if workflow is None:
            raise RuntimeError(f"Workflow advance did not persist workflow execution {workflow_id}")
        status_payload = _workflow_status_payload(workflow=workflow)
        return HandlerWorkflowAdvanceResult(
            handled=result.handled,
            reason=result.reason,
            status=status_payload["status"] or "running",
            active_run_id=status_payload["active_run_id"],
            last_error=status_payload["last_error"],
        )


@activity.defn(name="retry_handler_workflow_operation_activity")
def retry_handler_workflow_operation_activity(payload: WorkflowOperationRetryInput) -> WorkflowOperationRetryResult:
    settings = get_settings()
    session_factory = create_session_factory()
    with session_factory() as session:
        workflow = session.get(WorkflowExecution, str(payload.workflow_id or "").strip())
        if workflow is None:
            raise RuntimeError(f"Workflow retry is missing workflow {payload.workflow_id}")
        operation = session.get(WorkflowOperation, str(payload.operation_id or "").strip())
        if operation is None or operation.workflow_id != workflow.workflow_id:
            raise RuntimeError(f"Workflow retry is missing operation {payload.operation_id}")
        tenant = session.get(Tenant, workflow.tenant_id)
        if tenant is None:
            raise RuntimeError(f"Workflow retry is missing tenant {workflow.tenant_id}")
        handler_registry = build_workflow_handler_registry(
            integration_router=_build_workflow_integration_router(),
            extract_changed_fields_fn=extract_changed_fields,
            extract_status_transition_fn=extract_status_transition,
            build_runtime_for_selector_fn=build_runtime_for_selector,
            seed_issues_with_runtime_fn=seed_issues_with_runtime,
            post_jira_comment_fn=post_jira_comment,
            create_jira_comment_fn=create_jira_comment,
        )
        try:
            handle = execute_workflow_operation_retry(
                session=session,
                settings=settings,
                session_factory=session_factory,
                workflow=workflow,
                operation=operation,
                resolve_operation_retry_handler_fn=handler_registry.resolve_operation_retry_handler,
            )
        except (InvalidWorkflowOperationRetryError, UnsupportedWorkflowOperationRetryError) as exc:
            raise ApplicationError(
                str(exc),
                type="terminal_workflow_operation_retry_error",
                non_retryable=True,
            ) from exc
        session.commit()
        refreshed_workflow = session.get(WorkflowExecution, workflow.workflow_id)
        if refreshed_workflow is None:
            raise RuntimeError(f"Workflow retry lost workflow {workflow.workflow_id}")
        status_payload = _workflow_status_payload(workflow=refreshed_workflow)
        return WorkflowOperationRetryResult(
            operation_id=handle.operation_id,
            workflow_id=handle.workflow_id,
            operation_type=handle.operation_type,
            operation_status=handle.status,
            workflow_status=status_payload["status"] or "running",
            active_run_id=status_payload["active_run_id"],
            last_error=status_payload["last_error"],
        )
