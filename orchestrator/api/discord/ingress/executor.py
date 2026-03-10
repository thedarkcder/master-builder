from __future__ import annotations

import re
from typing import Literal

from sqlalchemy.orm import Session

from orchestrator.api.commands.executor_registry import register_tenant_command_executor
from orchestrator.api.discord.ask.board_service import ask_board_message as _ask_board_message_impl
from orchestrator.api.discord.ask.context import (
    project_filter_jql as _project_filter_jql,
    search_jira_issues_for_tenant as _search_jira_issues_for_tenant,
    tenant_project_keys as _tenant_project_keys,
)
from orchestrator.api.discord.ask.memory import (
    MAX_ASK_HISTORY_CONTEXT as ASK_HISTORY_CONTEXT_LIMIT,
    collect_ask_context_with_history_context as _collect_ask_context_with_history_context_impl,
    consume_pending_ask_action as _consume_pending_ask_action_impl,
    drop_issue_key_from_ask_history as _drop_issue_key_from_ask_history_impl,
    prune_missing_issue_keys_from_ask_history as _prune_missing_issue_keys_from_ask_history_impl,
    recent_ask_history as _recent_ask_history_impl,
    store_ask_history_entry as _store_ask_history_entry_impl,
    store_pending_ask_action as _store_pending_ask_action_impl,
    tenant_ask_history as _tenant_ask_history_impl,
)
from orchestrator.api.discord.ask.query_service import collect_ask_context as _collect_ask_context_impl
from orchestrator.api.discord.ingress import bug_runtime, gap_runtime, jira_runtime, seed_runtime
from orchestrator.api.discord.ingress.github_context import collect_github_ask_context as _collect_github_ask_context_impl
from orchestrator.api.discord.ingress.service import execute_tenant_command_ingress as _execute_tenant_command_ingress
from orchestrator.api.discord.ingress.wiring import build_discord_ingress_dependencies
from orchestrator.api.discord.shared.channel_scope_repository import SqlAlchemyDiscordChannelScopeRepository
from orchestrator.api.discord.shared.execution_policy import ensure_issue_is_executable as _ensure_issue_is_executable_impl
from orchestrator.api.discord.shared.scope_service import (
    normalize_scope_channel_id as _normalize_scope_channel_id_impl,
    resolve_command_scope as _resolve_command_scope_impl,
    resolve_project_for_issue as _resolve_project_for_issue_impl,
)
from orchestrator.api.discord.shared.state import (
    assert_channel_scope as _assert_channel_scope,
    assert_sensitive_command_permission as _assert_sensitive_command_permission,
    clear_seed_followup_context as _clear_seed_followup_context,
    find_seed_followup_context as _find_seed_followup_context,
    normalize_status_name as _normalize_status_name,
    store_seed_followup_context as _store_seed_followup_context,
)
from orchestrator.api.schemas import DiscordCommandRequest, DiscordCommandResponse
from orchestrator.core.codex_agents import answer_board_question_with_codex
from orchestrator.core.codex_runtime import CodexRuntimeError, build_codex_runtime
from orchestrator.core.codex_working_dir import resolve_codex_working_dir as _resolve_codex_working_dir_impl
from orchestrator.core.communications.command_pipeline import CommandScope
from orchestrator.core.config import get_settings
from orchestrator.core.project_routing import find_active_project_for_issue_key
from orchestrator.core.runs import RUN_STATUS_BLOCKED, RUN_STATUS_CANCELLED, RUN_STATUS_FAILED
from orchestrator.core.platform_secret_service import resolve_platform_secret_ref
from orchestrator.core.tenant_secret_service import resolve_scoped_secret_ref
from orchestrator.storage.models import Project, Tenant
from orchestrator.tools.github_app import github_client_from_tenant_config
from orchestrator.tools.project_repo_checkout import collect_local_repo_context

_channel_scope_repository = SqlAlchemyDiscordChannelScopeRepository()

RETRYABLE_STATUSES = {RUN_STATUS_FAILED, RUN_STATUS_BLOCKED, RUN_STATUS_CANCELLED}
ISSUE_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")
MAX_ASK_HISTORY_CONTEXT = ASK_HISTORY_CONTEXT_LIMIT


def _normalize_scope_channel_id(channel_id: str | None) -> str | None:
    return _normalize_scope_channel_id_impl(channel_id)



def _ensure_issue_is_executable(
    *,
    issue_status: str,
    tenant: Tenant,
    extra_executable_statuses: list[str] | tuple[str, ...] | None = None,
) -> None:
    _ensure_issue_is_executable_impl(
        issue_status=issue_status,
        tenant=tenant,
        normalize_status_name_fn=_normalize_status_name,
        extra_executable_statuses=extra_executable_statuses,
    )



def _resolve_command_scope(*, session: Session, tenant: Tenant, channel_id: str | None) -> CommandScope:
    return _resolve_command_scope_impl(
        session=session,
        tenant=tenant,
        channel_id=channel_id,
        channel_scope_repository=_channel_scope_repository,
        tenant_project_keys_fn=_tenant_project_keys,
    )



def _resolve_project_for_issue(*, session: Session, tenant: Tenant, issue_key: str) -> Project:
    return _resolve_project_for_issue_impl(
        session=session,
        tenant=tenant,
        issue_key=issue_key,
        find_active_project_for_issue_key_fn=find_active_project_for_issue_key,
    )



def _collect_ask_context(
    *,
    session: Session,
    tenant: Tenant,
    channel_id: str | None,
    question: str,
    scoped_issue_key: str | None = None,
) -> tuple[str | None, str | None, list[dict], dict[str, int]]:
    return _collect_ask_context_impl(
        session=session,
        tenant=tenant,
        channel_id=channel_id,
        question=question,
        scoped_issue_key=scoped_issue_key,
        project_filter_jql_fn=_project_filter_jql,
        search_issues_fn=_search_jira_issues_for_tenant,
    )



def _drop_issue_key_from_ask_history(
    *,
    session: Session,
    tenant: Tenant,
    user_id: str,
    channel_id: str,
    issue_key: str,
) -> None:
    _drop_issue_key_from_ask_history_impl(
        session=session,
        tenant=tenant,
        user_id=user_id,
        channel_id=channel_id,
        issue_key=issue_key,
    )



def _existing_issue_keys_for_tenant(
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



def _prune_missing_issue_keys_from_ask_history(
    *,
    session: Session,
    tenant: Tenant,
    user_id: str,
    channel_id: str,
) -> int:
    return _prune_missing_issue_keys_from_ask_history_impl(
        session=session,
        tenant=tenant,
        user_id=user_id,
        channel_id=channel_id,
        existing_issue_keys_fn=_existing_issue_keys_for_tenant,
    )



def _collect_ask_context_with_history_context(
    *,
    session: Session,
    tenant: Tenant,
    user_id: str,
    channel_id: str,
    question: str,
    scoped_issue_key: str | None,
) -> tuple[str | None, str | None, list[dict], dict[str, int], list[dict]]:
    return _collect_ask_context_with_history_context_impl(
        session=session,
        tenant=tenant,
        user_id=user_id,
        channel_id=channel_id,
        question=question,
        scoped_issue_key=scoped_issue_key,
        collect_ask_context_fn=_collect_ask_context,
        existing_issue_keys_fn=_existing_issue_keys_for_tenant,
    )



def _collect_github_ask_context(*, session: Session, tenant: Tenant, project_keys: list[str]) -> dict:
    return _collect_github_ask_context_impl(
        session=session,
        tenant=tenant,
        project_keys=project_keys,
        get_settings_fn=get_settings,
        resolve_scoped_secret_ref_fn=resolve_scoped_secret_ref,
        resolve_platform_secret_ref_fn=resolve_platform_secret_ref,
        github_client_from_tenant_config_fn=github_client_from_tenant_config,
        collect_local_repo_context_fn=collect_local_repo_context,
    )



def _store_pending_ask_action(
    *,
    session: Session,
    tenant: Tenant,
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



def consume_pending_ask_action(*, session: Session, tenant: Tenant, request_id: str) -> dict | None:
    return _consume_pending_ask_action_impl(
        session=session,
        tenant=tenant,
        request_id=request_id,
    )



def _tenant_ask_history(tenant: Tenant) -> list[dict]:
    return _tenant_ask_history_impl(tenant=tenant)



def _recent_ask_history(
    *,
    tenant: Tenant,
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



def _store_ask_history_entry(
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



def _ask_board_message(
    *,
    session: Session,
    tenant: Tenant,
    user_id: str,
    channel_id: str,
    question: str,
    scoped_issue_key: str | None = None,
) -> tuple[str, dict]:
    return _ask_board_message_impl(
        session=session,
        tenant=tenant,
        user_id=user_id,
        channel_id=channel_id,
        question=question,
        scoped_issue_key=scoped_issue_key,
        collect_ask_context_with_history_context_fn=_collect_ask_context_with_history_context,
        get_settings_fn=get_settings,
        build_codex_runtime_fn=build_codex_runtime,
        tenant_project_keys_fn=_tenant_project_keys,
        resolve_codex_working_dir_fn=_resolve_codex_working_dir,
        normalize_scope_channel_id_fn=_normalize_scope_channel_id,
        channel_scope_repository=_channel_scope_repository,
        answer_board_question_with_codex_fn=answer_board_question_with_codex,
        collect_github_ask_context_fn=_collect_github_ask_context,
        codex_runtime_error_type=CodexRuntimeError,
        store_ask_history_entry_fn=_store_ask_history_entry,
    )



def _resolve_codex_working_dir(
    *,
    session: Session,
    tenant: Tenant,
    settings,  # noqa: ANN001
    project_id: str | None = None,
    project_keys: list[str] | tuple[str, ...] | None = None,
) -> str:
    return _resolve_codex_working_dir_impl(
        session=session,
        tenant=tenant,
        settings=settings,
        project_id=project_id,
        project_keys=project_keys,
    )



def _build_ingress_dependencies():
    return build_discord_ingress_dependencies(
        issue_key_pattern=ISSUE_KEY_PATTERN,
        retryable_statuses=RETRYABLE_STATUSES,
        get_tenant_fn=lambda db, current_tenant_id: db.get(Tenant, current_tenant_id),
        assert_channel_scope_fn=lambda db, current_tenant, channel_id: _assert_channel_scope(
            session=db,
            tenant=current_tenant,
            channel_id=channel_id,
        ),
        assert_sensitive_command_permission_fn=lambda db, current_tenant, command_name, user_id, channel_id: _assert_sensitive_command_permission(
            session=db,
            tenant=current_tenant,
            command_name=command_name,
            user_id=user_id,
            channel_id=channel_id,
        ),
        resolve_scope_fn=lambda db, current_tenant, channel_id: _resolve_command_scope(
            session=db,
            tenant=current_tenant,
            channel_id=channel_id,
        ),
        collect_ask_context_with_history_context_fn=_collect_ask_context_with_history_context,
        collect_github_ask_context_fn=_collect_github_ask_context,
        store_pending_ask_action_fn=_store_pending_ask_action,
        store_ask_history_entry_fn=_store_ask_history_entry,
        ask_board_message_fn=_ask_board_message,
        run_gap_analysis_fn=gap_runtime.run_gap_analysis,
        normalize_discord_attachments_fn=bug_runtime.normalize_discord_attachments,
        create_discord_bug_issue_fn=bug_runtime.create_discord_bug_issue,
        seed_issues_with_codex_fn=seed_runtime.seed_issues_with_codex,
        find_seed_followup_context_fn=_find_seed_followup_context,
        store_seed_followup_context_fn=_store_seed_followup_context,
        clear_seed_followup_context_fn=_clear_seed_followup_context,
        validate_seed_followup_context_fn=seed_runtime.validate_seed_followup_context,
        resolve_project_for_issue_fn=_resolve_project_for_issue,
        fetch_issue_preview_fn=jira_runtime.fetch_jira_issue_preview,
        fetch_issue_detail_fn=jira_runtime.fetch_jira_issue_detail,
        settings_factory_fn=get_settings,
        build_codex_runtime_fn=build_codex_runtime,
        tenant_jira_oauth_context_fn=jira_runtime.tenant_jira_oauth_context,
        ensure_issue_is_executable_fn=_ensure_issue_is_executable,
        resolve_codex_working_dir_fn=_resolve_codex_working_dir,
    )



def execute_tenant_command_ingress(
    tenant_id: str,
    payload: DiscordCommandRequest,
    session: Session,
    *,
    defer_seed_issues: bool = False,
    require_ask_confirmation: bool = False,
    allow_plain_ask: bool = False,
    ingress_source: Literal["discord", "jira_comment"] = "discord",
) -> DiscordCommandResponse:
    return _execute_tenant_command_ingress(
        tenant_id=tenant_id,
        payload=payload,
        session=session,
        defer_seed_issues=defer_seed_issues,
        require_ask_confirmation=require_ask_confirmation,
        allow_plain_ask=allow_plain_ask,
        ingress_source=ingress_source,
        deps=_build_ingress_dependencies(),
    )



def execute_discord_command(
    tenant_id: str,
    payload: DiscordCommandRequest,
    session: Session,
    *,
    defer_seed_issues: bool = False,
    require_ask_confirmation: bool = False,
    allow_plain_ask: bool = False,
    ingress_source: Literal["discord", "jira_comment"] = "discord",
) -> DiscordCommandResponse:
    return execute_tenant_command_ingress(
        tenant_id=tenant_id,
        payload=payload,
        session=session,
        defer_seed_issues=defer_seed_issues,
        require_ask_confirmation=require_ask_confirmation,
        allow_plain_ask=allow_plain_ask,
        ingress_source=ingress_source,
    )



def register_discord_command_executor() -> None:
    register_tenant_command_executor(execute_tenant_command_ingress)
