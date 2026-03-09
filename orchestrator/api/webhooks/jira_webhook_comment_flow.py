from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy.orm import Session

from orchestrator.api.commands.entrypoint import execute_tenant_jira_comment_command
from orchestrator.api.discord.ask.context import remove_issue_key_from_tenant_ask_history
from orchestrator.api.discord.shared.state import remove_issue_key_from_seed_followups
from orchestrator.api.schemas import DiscordCommandRequest
from orchestrator.api.webhooks.contracts import (
    JIRA_COMMENT_EVENTS,
    extract_jira_comment_author_account_id,
    post_jira_comment,
)
from orchestrator.api.webhooks.jira_webhook_types import JiraWebhookContext, jira_webhook_response

logger = logging.getLogger(__name__)

execute_jira_comment_command = execute_tenant_jira_comment_command


def stage_handle_issue_deleted(
    *,
    context: JiraWebhookContext,
    session: Session,
) -> dict | None:
    if context.webhook_event != "issue_deleted":
        return None
    removed_entries = remove_issue_key_from_tenant_ask_history(
        session=session,
        tenant=context.tenant,
        issue_key=context.issue_key,
    )
    removed_seed_contexts, removed_seed_issue_refs = remove_issue_key_from_seed_followups(
        session=session,
        tenant=context.tenant,
        issue_key=context.issue_key,
    )
    logger.info(
        "jira_webhook_issue_deleted request_id=%s tenant_id=%s issue_key=%s removed_history_entries=%s removed_seed_contexts=%s removed_seed_issue_refs=%s",
        context.request_id,
        context.tenant_id,
        context.issue_key,
        removed_entries,
        removed_seed_contexts,
        removed_seed_issue_refs,
    )
    return jira_webhook_response(
        context,
        enqueued=False,
        reason="issue_deleted",
        removed_history_entries=removed_entries,
        removed_seed_contexts=removed_seed_contexts,
        removed_seed_issue_refs=removed_seed_issue_refs,
        webhook_event=context.webhook_event,
    )


def stage_handle_invalid_comment_command(
    *,
    context: JiraWebhookContext,
) -> dict | None:
    if not context.comment_command_error:
        return None
    logger.info(
        "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=invalid_comment_command",
        context.request_id,
        context.tenant_id,
        context.issue_key,
    )
    return jira_webhook_response(
        context,
        enqueued=False,
        reason="invalid_comment_command",
        webhook_event=context.webhook_event,
    )


def stage_handle_comment_event_memory(
    *,
    context: JiraWebhookContext,
    session: Session,
) -> int:
    if context.webhook_event not in JIRA_COMMENT_EVENTS:
        return 0
    removed_entries = remove_issue_key_from_tenant_ask_history(
        session=session,
        tenant=context.tenant,
        issue_key=context.issue_key,
    )
    logger.info(
        "jira_webhook_comment_event_memory_cleared request_id=%s tenant_id=%s issue_key=%s webhook_event=%s removed_history_entries=%s",
        context.request_id,
        context.tenant_id,
        context.issue_key,
        context.webhook_event,
        removed_entries,
    )
    return removed_entries


def stage_handle_comment_without_command(
    *,
    context: JiraWebhookContext,
    removed_history_entries: int,
) -> dict | None:
    if context.webhook_event not in JIRA_COMMENT_EVENTS or context.comment_command is not None:
        return None
    logger.info(
        "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=comment_without_command webhook_event=%s",
        context.request_id,
        context.tenant_id,
        context.issue_key,
        context.webhook_event,
    )
    return jira_webhook_response(
        context,
        enqueued=False,
        reason="comment_without_command",
        webhook_event=context.webhook_event,
        removed_history_entries=removed_history_entries,
    )


def stage_handle_comment_ask_command(
    *,
    context: JiraWebhookContext,
    session: Session,
    settings,  # noqa: ANN001
) -> dict | None:
    if context.comment_command != "ask":
        return None
    question = (context.comment_command_argument or "").strip()
    if not question:
        return jira_webhook_response(
            context,
            enqueued=False,
            reason="invalid_comment_command",
        )
    author_account_id = extract_jira_comment_author_account_id(context.payload) or "jira-user"
    try:
        ask_response = execute_jira_comment_command(
            session=session,
            tenant_id=context.tenant_id,
            payload=DiscordCommandRequest(
                user_id=author_account_id,
                channel_id=None,
                command=f"!ask @{context.issue_key} {question}",
            ),
        )
        response_text = ask_response.message.strip()
        if not response_text:
            response_text = "I processed your question but returned no response text."
    except HTTPException as exc:
        logger.exception(
            "jira_comment_ask_command_failed request_id=%s tenant_id=%s issue_key=%s detail=%s error=%s",
            context.request_id,
            context.tenant_id,
            context.issue_key,
            exc.detail,
            exc,
        )
        response_text = f"Unable to process `/mb ask`: {exc.detail}"
    posted, post_error = post_jira_comment(
        session=session,
        tenant=context.tenant,
        issue_key=context.issue_key,
        comment=response_text,
        settings=settings,
    )
    return jira_webhook_response(
        context,
        enqueued=False,
        reason="comment_command_ask",
        command=context.comment_command,
        question=question,
        comment_posted=posted,
        comment_error=post_error,
        webhook_event=context.webhook_event,
    )
