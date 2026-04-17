from __future__ import annotations

from sqlalchemy.orm import Session

from orchestrator.api.discord.ingress.seed_runtime import seed_issues_with_runtime
from orchestrator.api.discord.seed.issue_service import list_child_issue_previews_for_parent
from orchestrator.api.jira_oauth.connection_service import tenant_jira_oauth_context
from orchestrator.api.webhooks.contracts import (
    create_jira_comment,
    extract_changed_fields,
    extract_jira_comment_id,
    extract_jira_comment_text,
    extract_status_transition,
    post_jira_comment,
)
from orchestrator.api.webhooks.jira_webhook_types import JiraWebhookContext, jira_webhook_response
from orchestrator.core.agent_runtime_resolver import build_runtime_for_selector
from orchestrator.core.codex_agents import classify_engineering_clarification_with_codex
from orchestrator.core.jira_parent_child_sync_service import (
    JiraParentChildSyncContext,
    JiraParentChildSyncResult,
    handle_engineering_clarification_command as handle_engineering_clarification_command_service,
    handle_engineering_clarification_reply as handle_engineering_clarification_reply_service,
    handle_pm_interview_reply as handle_pm_interview_reply_service,
    handle_parent_feature_sync as handle_parent_feature_sync_service,
    is_system_generated_comment as is_system_generated_comment_service,
)

def _build_service_context(*, context: JiraWebhookContext) -> JiraParentChildSyncContext:
    return JiraParentChildSyncContext(
        request_id=context.request_id,
        tenant_id=context.tenant_id,
        tenant=context.tenant,
        project_id=context.project.project_id if context.project is not None else None,
        issue_key=context.issue_key,
        issue_labels=list(context.issue_labels or []),
        payload=dict(context.payload),
        webhook_event=context.webhook_event,
        comment_command=context.comment_command,
        comment_command_argument=context.comment_command_argument,
    )


def _webhook_response_from_result(
    *,
    context: JiraWebhookContext,
    result: JiraParentChildSyncResult,
) -> dict | None:
    if not result.handled:
        return None
    return jira_webhook_response(
        context,
        enqueued=False,
        reason=result.reason,
        **result.extra,
    )


def is_system_generated_comment(*, text: str | None) -> bool:
    return is_system_generated_comment_service(text=text)


def handle_parent_feature_sync(
    *,
    context: JiraWebhookContext,
    session: Session,
    settings,  # noqa: ANN001
) -> dict | None:
    result = handle_parent_feature_sync_service(
        context=_build_service_context(context=context),
        session=session,
        settings=settings,
        tenant_jira_oauth_context_fn=tenant_jira_oauth_context,
        extract_changed_fields_fn=extract_changed_fields,
        extract_status_transition_fn=extract_status_transition,
        list_child_issue_previews_for_parent_fn=list_child_issue_previews_for_parent,
        build_runtime_for_selector_fn=build_runtime_for_selector,
        seed_issues_with_runtime_fn=seed_issues_with_runtime,
        post_jira_comment_fn=post_jira_comment,
        create_jira_comment_fn=create_jira_comment,
    )
    return _webhook_response_from_result(context=context, result=result)


def handle_engineering_clarification_command(
    *,
    context: JiraWebhookContext,
    session: Session,
    settings,  # noqa: ANN001
) -> dict | None:
    result = handle_engineering_clarification_command_service(
        context=_build_service_context(context=context),
        session=session,
        settings=settings,
        tenant_jira_oauth_context_fn=tenant_jira_oauth_context,
        build_runtime_for_selector_fn=build_runtime_for_selector,
        classify_engineering_clarification_with_codex_fn=classify_engineering_clarification_with_codex,
        post_jira_comment_fn=post_jira_comment,
    )
    return _webhook_response_from_result(context=context, result=result)


def handle_engineering_clarification_reply(
    *,
    context: JiraWebhookContext,
    session: Session,
    settings,  # noqa: ANN001
) -> dict | None:
    result = handle_engineering_clarification_reply_service(
        context=_build_service_context(context=context),
        session=session,
        settings=settings,
        tenant_jira_oauth_context_fn=tenant_jira_oauth_context,
        seed_issues_with_runtime_fn=seed_issues_with_runtime,
        post_jira_comment_fn=post_jira_comment,
        create_jira_comment_fn=create_jira_comment,
        extract_jira_comment_text_fn=extract_jira_comment_text,
    )
    return _webhook_response_from_result(context=context, result=result)


def handle_pm_interview_reply(
    *,
    context: JiraWebhookContext,
    session: Session,
    settings,  # noqa: ANN001
) -> dict | None:
    result = handle_pm_interview_reply_service(
        context=_build_service_context(context=context),
        session=session,
        settings=settings,
        tenant_jira_oauth_context_fn=tenant_jira_oauth_context,
        build_runtime_for_selector_fn=build_runtime_for_selector,
        seed_issues_with_runtime_fn=seed_issues_with_runtime,
        post_jira_comment_fn=post_jira_comment,
        create_jira_comment_fn=create_jira_comment,
        extract_jira_comment_text_fn=extract_jira_comment_text,
        extract_jira_comment_id_fn=extract_jira_comment_id,
    )
    return _webhook_response_from_result(context=context, result=result)
