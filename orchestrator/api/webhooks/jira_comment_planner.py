from __future__ import annotations

from dataclasses import dataclass

from orchestrator.api.webhooks import jira_webhook_comment_flow
from orchestrator.api.webhooks.jira_webhook_types import JiraWebhookContext


@dataclass(frozen=True)
class JiraCommentPlan:
    content: dict | None = None
    removed_history_entries: int = 0


def plan_jira_comment_flow(
    *,
    context: JiraWebhookContext,
    session,
    settings,  # noqa: ANN001
) -> JiraCommentPlan:
    deleted_response = jira_webhook_comment_flow.stage_handle_issue_deleted(
        context=context,
        session=session,
    )
    if deleted_response is not None:
        return JiraCommentPlan(content=deleted_response)

    invalid_comment_response = jira_webhook_comment_flow.stage_handle_invalid_comment_command(
        context=context
    )
    if invalid_comment_response is not None:
        return JiraCommentPlan(content=invalid_comment_response)

    removed_history_entries = jira_webhook_comment_flow.stage_handle_comment_event_memory(
        context=context,
        session=session,
    )
    comment_ask_response = jira_webhook_comment_flow.stage_handle_comment_ask_command(
        context=context,
        session=session,
        settings=settings,
    )
    if comment_ask_response is not None:
        return JiraCommentPlan(
            content=comment_ask_response,
            removed_history_entries=removed_history_entries,
        )

    comment_reply_response = jira_webhook_comment_flow.stage_handle_comment_decision_reply(
        context=context,
        session=session,
        settings=settings,
    )
    if comment_reply_response is not None:
        return JiraCommentPlan(
            content=comment_reply_response,
            removed_history_entries=removed_history_entries,
        )

    comment_without_command_response = jira_webhook_comment_flow.stage_handle_comment_without_command(
        context=context,
        removed_history_entries=removed_history_entries,
    )
    if comment_without_command_response is not None:
        return JiraCommentPlan(
            content=comment_without_command_response,
            removed_history_entries=removed_history_entries,
        )

    return JiraCommentPlan(removed_history_entries=removed_history_entries)
