from __future__ import annotations

from uuid import uuid4

from sqlalchemy.orm import Session

from orchestrator.api.discord.ask.context import (
    project_filter_jql as _project_filter_jql,
    search_jira_issues_for_tenant as _search_jira_issues_for_tenant,
)
from orchestrator.api.discord.ask.query_service import collect_ask_context as _collect_ask_context_query_service
from orchestrator.api.discord.ask.history_service import DiscordAskHistoryService
from orchestrator.storage.models import Tenant

MAX_PENDING_ASK_ACTIONS = 50
MAX_ASK_HISTORY_ENTRIES = 80
MAX_ASK_HISTORY_CONTEXT = 6

_ask_history_service = DiscordAskHistoryService(
    max_pending_actions=MAX_PENDING_ASK_ACTIONS,
    max_history_entries=MAX_ASK_HISTORY_ENTRIES,
    max_history_context=MAX_ASK_HISTORY_CONTEXT,
)


def collect_ask_context(
    *,
    session: Session,
    tenant: Tenant,
    channel_id: str | None,
    question: str,
    scoped_issue_key: str | None = None,
) -> tuple[str | None, str | None, list[dict], dict[str, int]]:
    return _collect_ask_context_query_service(
        session=session,
        tenant=tenant,
        channel_id=channel_id,
        question=question,
        scoped_issue_key=scoped_issue_key,
        project_filter_jql_fn=_project_filter_jql,
        search_issues_fn=_search_jira_issues_for_tenant,
    )


def remove_issue_key_from_tenant_ask_history(
    *,
    session: Session,
    tenant: Tenant,
    issue_key: str,
    user_id: str | None = None,
    channel_id: str | None = None,
) -> int:
    return _ask_history_service.remove_issue_key_from_ask_history(
        session=session,
        tenant=tenant,
        issue_key=issue_key,
        user_id=user_id,
        channel_id=channel_id,
    )


def drop_issue_key_from_ask_history(
    *,
    session: Session,
    tenant: Tenant,
    user_id: str,
    channel_id: str,
    issue_key: str,
) -> None:
    remove_issue_key_from_tenant_ask_history(
        session=session,
        tenant=tenant,
        issue_key=issue_key,
        user_id=user_id,
        channel_id=channel_id,
    )


def existing_issue_keys_for_tenant(
    *,
    session: Session,
    tenant: Tenant,
    channel_id: str | None,
    issue_keys: set[str],
) -> set[str]:
    if not issue_keys:
        return set()

    normalized_issue_keys = sorted({value.strip().upper() for value in issue_keys if value and value.strip()})[:100]
    if not normalized_issue_keys:
        return set()

    quoted_issue_keys = ", ".join(f'"{value}"' for value in normalized_issue_keys)
    jql = f"{_project_filter_jql(session=session, tenant=tenant, channel_id=channel_id)} AND key in ({quoted_issue_keys})"
    issues = _search_jira_issues_for_tenant(
        session=session,
        tenant=tenant,
        jql=jql,
        max_results=len(normalized_issue_keys),
    )
    return {str(issue.key or "").strip().upper() for issue in issues if str(issue.key or "").strip()}


def prune_missing_issue_keys_from_ask_history(
    *,
    session: Session,
    tenant: Tenant,
    user_id: str,
    channel_id: str,
    existing_issue_keys_fn=None,  # noqa: ANN001
) -> int:
    resolver = existing_issue_keys_fn or existing_issue_keys_for_tenant
    return _ask_history_service.prune_missing_issue_keys_from_ask_history(
        session=session,
        tenant=tenant,
        user_id=user_id,
        channel_id=channel_id,
        existing_issue_keys_fn=resolver,
    )


def collect_ask_context_with_history_context(
    *,
    session: Session,
    tenant: Tenant,
    user_id: str,
    channel_id: str,
    question: str,
    scoped_issue_key: str | None,
    collect_ask_context_fn=None,  # noqa: ANN001
    existing_issue_keys_fn=None,  # noqa: ANN001
    prune_history: bool = True,
) -> tuple[str | None, str | None, list[dict], dict[str, int], list[dict]]:
    collect_fn = collect_ask_context_fn or collect_ask_context
    existing_fn = existing_issue_keys_fn or existing_issue_keys_for_tenant
    return _ask_history_service.collect_ask_context_with_history_context(
        session=session,
        tenant=tenant,
        user_id=user_id,
        channel_id=channel_id,
        question=question,
        scoped_issue_key=scoped_issue_key,
        collect_ask_context_fn=collect_fn,
        existing_issue_keys_fn=existing_fn,
        prune_history=prune_history,
    )


def store_pending_ask_action(
    *,
    session: Session,
    tenant: Tenant,
    user_id: str,
    channel_id: str | None,
    question: str,
    summary: str,
    proposed_command: str,
) -> dict:
    request_id = uuid4().hex
    return _ask_history_service.store_pending_ask_action(
        session=session,
        tenant=tenant,
        request_id=request_id,
        user_id=user_id,
        channel_id=channel_id,
        question=question,
        summary=summary,
        proposed_command=proposed_command,
    )


def consume_pending_ask_action(
    *,
    session: Session,
    tenant: Tenant,
    request_id: str,
) -> dict | None:
    return _ask_history_service.consume_pending_ask_action(
        session=session,
        tenant=tenant,
        request_id=request_id,
    )


def tenant_ask_history(tenant: Tenant) -> list[dict]:
    return _ask_history_service.tenant_ask_history(tenant=tenant)


def recent_ask_history(
    *,
    tenant: Tenant,
    user_id: str,
    channel_id: str,
    limit: int = MAX_ASK_HISTORY_CONTEXT,
) -> list[dict]:
    return _ask_history_service.recent_ask_history(
        tenant=tenant,
        user_id=user_id,
        channel_id=channel_id,
        limit=limit,
    )


def store_ask_history_entry(
    *,
    session: Session,
    tenant: Tenant,
    user_id: str,
    channel_id: str,
    question: str,
    answer: str,
    issue_key: str | None,
    status_name: str | None,
) -> None:
    _ask_history_service.store_ask_history_entry(
        session=session,
        tenant=tenant,
        user_id=user_id,
        channel_id=channel_id,
        question=question,
        answer=answer,
        issue_key=issue_key,
        status_name=status_name,
    )
