from __future__ import annotations

from dataclasses import dataclass

from orchestrator.api.webhooks import jira_webhook_comment_flow
from orchestrator.api.webhooks.jira_webhook_types import JiraWebhookContext
from orchestrator.core.decision_state_machine import (
    ExecutionAdmissionReason,
    build_execution_admission_block,
)
from orchestrator.core.communications.execution_admission_format import present_jira_admission


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
    if context.project is None and context.webhook_event in jira_webhook_comment_flow.JIRA_COMMENT_EVENTS:
        admission = build_execution_admission_block(
            reason=ExecutionAdmissionReason.PROJECT_NOT_MAPPED,
        )
        admission_presentation = present_jira_admission(admission=admission)
        return JiraCommentPlan(
            content=jira_webhook_comment_flow.jira_webhook_response(
                context,
                enqueued=False,
                command=context.comment_command,
                webhook_event=context.webhook_event,
                removed_history_entries=removed_history_entries,
                **admission_presentation.response_fields,
            ),
            removed_history_entries=removed_history_entries,
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

    comment_clarify_response = jira_webhook_comment_flow.stage_handle_comment_clarify_command(
        context=context,
        session=session,
        settings=settings,
    )
    if comment_clarify_response is not None:
        return JiraCommentPlan(
            content=comment_clarify_response,
            removed_history_entries=removed_history_entries,
        )

    engineering_reply_response = jira_webhook_comment_flow.stage_handle_comment_engineering_clarification_reply(
        context=context,
        session=session,
        settings=settings,
    )
    if engineering_reply_response is not None:
        return JiraCommentPlan(
            content=engineering_reply_response,
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
