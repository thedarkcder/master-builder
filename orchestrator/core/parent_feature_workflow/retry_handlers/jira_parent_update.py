from __future__ import annotations

from orchestrator.core.projects.architecture_document_service import ArchitectureDocumentService
from orchestrator.core.integrations.atlassian.links import architecture_document_remote_link_spec, workflow_execution_remote_link_spec
from orchestrator.core.integrations.atlassian.parent_child_sync_publishers import (
    update_issue_sync_label,
    upsert_jira_remote_link,
)
from orchestrator.core.parent_feature_workflow.dependencies import ParentFeatureWorkflowHandlerDeps
from orchestrator.core.parent_feature_workflow.operations import PARENT_OP_JIRA_PARENT_UPDATE
from orchestrator.core.parent_feature_workflow.retry_support import (
    ParentWorkflowRetryContext,
    jira_issue_key_for_workflow,
    operation_handle,
)
from orchestrator.core.workflow.advance import InvalidWorkflowOperationRetryError
from orchestrator.core.workflow.execution_projection import WorkflowExecutionProjection, classify_external_workflow_failure
from orchestrator.core.workflow.operation_service import WorkflowOperationHandle


class JiraParentUpdateRetryExecutor:
    operation_type = PARENT_OP_JIRA_PARENT_UPDATE

    def __init__(self, *, deps: ParentFeatureWorkflowHandlerDeps) -> None:
        self._deps = deps

    def execute(self, *, context: ParentWorkflowRetryContext) -> WorkflowOperationHandle:
        parent_issue_key = jira_issue_key_for_workflow(context.workflow)
        jira_adapter = self._deps.integration_router.jira(
            session=context.session,
            tenant=context.tenant,
            settings=context.settings,
        )
        oauth = jira_adapter.oauth_context
        parent_detail = jira_adapter.get_issue_detail(issue_id_or_key=parent_issue_key)
        architecture_gate = ArchitectureDocumentService(settings_factory=lambda: context.settings).resolve_gate(
            session=context.session,
            project=context.project,
            parent_issue_key=parent_issue_key,
            issue_summary=parent_detail.summary,
            issue_labels=list(parent_detail.labels or []),
            actor="system",
        )
        if architecture_gate.required and architecture_gate.document is None:
            raise InvalidWorkflowOperationRetryError(
                architecture_gate.block_reason or f"Architecture document link is required for {parent_issue_key}"
            )
        architecture_document = architecture_gate.document
        lifecycle = WorkflowExecutionProjection(
            session=context.session,
            workflow=context.workflow,
            workflow_type=context.workflow_type,
        )
        operation, attempt = lifecycle.start_operation_attempt(operation_type=context.operation.operation_type)
        try:
            update_issue_sync_label(
                oauth=oauth,
                parent_detail=parent_detail,
                target_label="children_syncing",
            )
            upsert_jira_remote_link(
                oauth=oauth,
                issue_key=parent_issue_key,
                spec=workflow_execution_remote_link_spec(
                    admin_ui_base_url=context.settings.admin_ui_base_url,
                    workflow=context.workflow,
                ),
            )
            if architecture_document is not None:
                title = str(architecture_document.title or "").strip()
                url = str(architecture_document.canonical_url or "").strip()
                if not title or not url:
                    raise InvalidWorkflowOperationRetryError(
                        f"Architecture document link is incomplete for {parent_issue_key}"
                    )
                upsert_jira_remote_link(
                    oauth=oauth,
                    issue_key=parent_issue_key,
                    spec=architecture_document_remote_link_spec(
                        issue_key=parent_issue_key,
                        title=title,
                        url=url,
                    ),
                )
        except Exception as exc:  # noqa: BLE001
            lifecycle.fail_started_operation(
                operation=operation,
                attempt=attempt,
                category=classify_external_workflow_failure(error=exc),
                message=str(exc),
            )
            return operation_handle(context=context, operation=operation)
        lifecycle.complete_started_operation(
            operation=operation,
            attempt=attempt,
            summary="Parent Jira metadata and reference links synced without modifying the description.",
        )
        return operation_handle(context=context, operation=operation)
