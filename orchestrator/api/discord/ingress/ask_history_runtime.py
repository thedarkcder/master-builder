from __future__ import annotations

from sqlalchemy.orm import Session

from orchestrator.api.discord.ask.context import (
    project_filter_jql,
    search_jira_issues_for_tenant,
)
from orchestrator.api.discord.ask.memory import (
    MAX_ASK_HISTORY_CONTEXT as ASK_HISTORY_CONTEXT_LIMIT,
    consume_pending_ask_action as _consume_pending_ask_action_impl,
    prune_missing_issue_keys_from_ask_history as _prune_missing_issue_keys_from_ask_history_impl,
    recent_ask_history as _recent_ask_history_impl,
    store_ask_history_entry as _store_ask_history_entry_impl,
    store_pending_ask_action as _store_pending_ask_action_impl,
    tenant_ask_history as _tenant_ask_history_impl,
)

MAX_ASK_HISTORY_CONTEXT = ASK_HISTORY_CONTEXT_LIMIT


def existing_issue_keys_for_tenant(
    *,
    session: Session,
    tenant,
    channel_id: str | None,
    issue_keys: set[str],
) -> set[str]:
    if not issue_keys:
        return set()

    normalized_issue_keys = sorted(
        {value.strip().upper() for value in issue_keys if value and value.strip()}
    )[:100]
    if not normalized_issue_keys:
        return set()

    quoted_issue_keys = ", ".join(f'"{value}"' for value in normalized_issue_keys)
    jql = f"{project_filter_jql(session=session, tenant=tenant, channel_id=channel_id)} AND key in ({quoted_issue_keys})"
    issues = search_jira_issues_for_tenant(
        session=session,
        tenant=tenant,
        jql=jql,
        max_results=len(normalized_issue_keys),
    )
    return {
        str(issue.key or "").strip().upper()
        for issue in issues
        if str(issue.key or "").strip()
    }


def prune_missing_issue_keys_from_ask_history(
    *,
    session: Session,
    tenant,
    user_id: str,
    channel_id: str,
) -> int:
    return _prune_missing_issue_keys_from_ask_history_impl(
        session=session,
        tenant=tenant,
        user_id=user_id,
        channel_id=channel_id,
        existing_issue_keys_fn=existing_issue_keys_for_tenant,
    )


def store_pending_ask_action(
    *,
    session: Session,
    tenant,
    user_id: str,
    channel_id: str | None,
    question: str,
    summary: str,
    proposed_command: str,
) -> dict:
    return _store_pending_ask_action_impl(
        session=session,
        tenant=tenant,
        user_id=user_id,
        channel_id=channel_id,
        question=question,
        summary=summary,
        proposed_command=proposed_command,
    )


def consume_pending_ask_action(
    *,
    session: Session,
    tenant,
    request_id: str,
) -> dict | None:
    return _consume_pending_ask_action_impl(
        session=session,
        tenant=tenant,
        request_id=request_id,
    )


def tenant_ask_history(*, tenant) -> list[dict]:
    return _tenant_ask_history_impl(tenant=tenant)


def recent_ask_history(
    *,
    tenant,
    user_id: str,
    channel_id: str,
    limit: int = MAX_ASK_HISTORY_CONTEXT,
) -> list[dict]:
    return _recent_ask_history_impl(
        tenant=tenant,
        user_id=user_id,
        channel_id=channel_id,
        limit=limit,
    )


def store_ask_history_entry(
    *,
    session: Session,
    tenant,
    user_id: str,
    channel_id: str,
    question: str,
    answer: str,
    issue_key: str | None,
    status_name: str | None,
) -> None:
    _store_ask_history_entry_impl(
        session=session,
        tenant=tenant,
        user_id=user_id,
        channel_id=channel_id,
        question=question,
        answer=answer,
        issue_key=issue_key,
        status_name=status_name,
    )
