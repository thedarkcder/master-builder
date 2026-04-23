from __future__ import annotations

from dataclasses import dataclass

from orchestrator.core.jira_parent_child_sync_service import (
    ParentFeatureWorkflowAdvanceHandler,
    ParentFeatureWorkflowHandlerDeps,
    ParentFeatureWorkflowOperationRetryHandler,
)
from orchestrator.core.workflow_advance import WorkflowAdvanceHandler, WorkflowOperationRetryHandler


@dataclass(frozen=True)
class WorkflowHandlerRegistry:
    advance_handlers: dict[str, WorkflowAdvanceHandler]
    operation_retry_handlers: dict[str, WorkflowOperationRetryHandler]

    def resolve_advance_handler(self, handler_key: str) -> WorkflowAdvanceHandler:
        normalized = str(handler_key or "").strip()
        handler = self.advance_handlers.get(normalized)
        if handler is None:
            raise LookupError(f"No workflow advance handler is registered for {handler_key}")
        return handler

    def resolve_operation_retry_handler(self, handler_key: str) -> WorkflowOperationRetryHandler:
        normalized = str(handler_key or "").strip()
        handler = self.operation_retry_handlers.get(normalized)
        if handler is None:
            raise LookupError(f"No workflow operation retry handler is registered for {handler_key}")
        return handler


def build_workflow_handler_registry(
    *,
    integration_router,
    extract_changed_fields_fn,
    extract_status_transition_fn,
    build_runtime_for_selector_fn,
    seed_issues_with_runtime_fn,
    post_jira_comment_fn,
    create_jira_comment_fn,
) -> WorkflowHandlerRegistry:
    parent_feature_deps = ParentFeatureWorkflowHandlerDeps(
        integration_router=integration_router,
        extract_changed_fields_fn=extract_changed_fields_fn,
        extract_status_transition_fn=extract_status_transition_fn,
        build_runtime_for_selector_fn=build_runtime_for_selector_fn,
        seed_issues_with_runtime_fn=seed_issues_with_runtime_fn,
        post_jira_comment_fn=post_jira_comment_fn,
        create_jira_comment_fn=create_jira_comment_fn,
    )
    parent_feature_advance_handler = ParentFeatureWorkflowAdvanceHandler(deps=parent_feature_deps)
    parent_feature_retry_handler = ParentFeatureWorkflowOperationRetryHandler(deps=parent_feature_deps)
    return WorkflowHandlerRegistry(
        advance_handlers={
            "jira_parent_feature": parent_feature_advance_handler,
        },
        operation_retry_handlers={
            "jira_parent_feature": parent_feature_retry_handler,
        },
    )
