from __future__ import annotations

from orchestrator.api.atlassian_oauth.connection_service import tenant_atlassian_oauth_context
from orchestrator.api.webhooks.contracts import (
    create_jira_comment,
    extract_changed_fields,
    extract_status_transition,
    post_jira_comment,
)
from orchestrator.core.issue_fanout.service import list_child_issue_previews_for_parent
from orchestrator.core.runtime.agent_runtime_resolver import build_runtime_for_selector
from orchestrator.core.workflow.handler_composition import build_installed_workflow_handler_registry
from orchestrator.core.workflow.handler_registry import WorkflowHandlerRegistry
from orchestrator.core.integrations.workflow.provider import (
    JiraWorkflowConnectionProvider,
    WorkflowIntegrationAdapterProvider,
)
from orchestrator.core.integrations.workflow.router import WorkflowIntegrationRouter
from orchestrator.runtime.issue_fanout import seed_issues_with_runtime


def build_runtime_workflow_handler_registry() -> WorkflowHandlerRegistry:
    integration_router = WorkflowIntegrationRouter(
        adapter_provider=WorkflowIntegrationAdapterProvider(
            jira_provider=JiraWorkflowConnectionProvider(
                oauth_context_resolver=tenant_atlassian_oauth_context,
                list_child_issue_previews_for_parent_fn=list_child_issue_previews_for_parent,
            )
        )
    )
    return build_installed_workflow_handler_registry(
        integration_router=integration_router,
        extract_changed_fields_fn=extract_changed_fields,
        extract_status_transition_fn=extract_status_transition,
        build_runtime_for_selector_fn=build_runtime_for_selector,
        seed_issues_with_runtime_fn=seed_issues_with_runtime,
        post_jira_comment_fn=post_jira_comment,
        create_jira_comment_fn=create_jira_comment,
    )
