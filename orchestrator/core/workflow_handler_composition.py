from __future__ import annotations

from orchestrator.core.parent_feature_workflow.dependencies import ParentFeatureWorkflowHandlerDeps
from orchestrator.core.parent_feature_workflow.handlers import ParentFeatureWorkflowAdvanceHandler
from orchestrator.core.parent_feature_workflow.retry import ParentFeatureWorkflowOperationRetryHandler
from orchestrator.core.workflow_handler_registry import WorkflowHandlerRegistry, build_workflow_handler_registry


def build_installed_workflow_handler_registry(
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
    return build_workflow_handler_registry(
        advance_handlers={
            "jira_parent_feature": ParentFeatureWorkflowAdvanceHandler(deps=parent_feature_deps),
        },
        operation_retry_handlers={
            "jira_parent_feature": ParentFeatureWorkflowOperationRetryHandler(deps=parent_feature_deps),
        },
    )
