from __future__ import annotations

from sqlalchemy.orm import Session

from orchestrator.api.discord.ask.board_service import ask_board_message as _ask_board_message_impl
from orchestrator.api.discord.ask.context import (
    project_filter_jql as _project_filter_jql,
    search_jira_issues_for_tenant as _search_jira_issues_for_tenant,
    tenant_project_keys as _tenant_project_keys,
)
from orchestrator.api.discord.ask.memory import (
    collect_ask_context_with_history_context as _collect_ask_context_with_history_context_impl,
    drop_issue_key_from_ask_history as _drop_issue_key_from_ask_history_impl,
)
from orchestrator.api.discord.ask.query_service import collect_ask_context as _collect_ask_context_impl
from orchestrator.api.discord.ingress import ask_history_runtime, github_context
from orchestrator.api.discord.shared.channel_scope_repository import SqlAlchemyDiscordChannelScopeRepository
from orchestrator.api.discord.shared.scope_service import normalize_scope_channel_id
from orchestrator.core.runtime.agents import answer_board_question_with_runtime
from orchestrator.core.runtime.runtime import CodexRuntimeError, build_codex_runtime
from orchestrator.core.runtime.working_dir import resolve_codex_working_dir
from orchestrator.core.config import get_settings
from orchestrator.core.platform.secret_service import resolve_platform_secret_ref
from orchestrator.core.platform.tenant_secret_service import resolve_scoped_secret_ref
from orchestrator.tools.github_app import github_client_from_tenant_config
from orchestrator.tools.project_repo_checkout import collect_local_repo_context

_channel_scope_repository = SqlAlchemyDiscordChannelScopeRepository()


def collect_ask_context(
    *,
    session: Session,
    tenant,
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


def drop_issue_key_from_ask_history(
    *,
    session: Session,
    tenant,
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


def collect_ask_context_with_history_context(
    *,
    session: Session,
    tenant,
    user_id: str,
    channel_id: str,
    question: str,
    scoped_issue_key: str | None,
    prune_history: bool = True,
) -> tuple[str | None, str | None, list[dict], dict[str, int], list[dict]]:
    return _collect_ask_context_with_history_context_impl(
        session=session,
        tenant=tenant,
        user_id=user_id,
        channel_id=channel_id,
        question=question,
        scoped_issue_key=scoped_issue_key,
        collect_ask_context_fn=collect_ask_context,
        existing_issue_keys_fn=ask_history_runtime.existing_issue_keys_for_tenant,
        prune_history=prune_history,
    )


def collect_github_ask_context(
    *,
    session: Session,
    tenant,
    project_keys: list[str],
    get_settings_fn=None,
    resolve_scoped_secret_ref_fn=None,
    resolve_platform_secret_ref_fn=None,
    github_client_from_tenant_config_fn=None,
    collect_local_repo_context_fn=None,
) -> dict:
    return github_context.collect_github_ask_context(
        session=session,
        tenant=tenant,
        project_keys=project_keys,
        get_settings_fn=get_settings_fn or get_settings,
        resolve_scoped_secret_ref_fn=resolve_scoped_secret_ref_fn or resolve_scoped_secret_ref,
        resolve_platform_secret_ref_fn=resolve_platform_secret_ref_fn or resolve_platform_secret_ref,
        github_client_from_tenant_config_fn=github_client_from_tenant_config_fn or github_client_from_tenant_config,
        collect_local_repo_context_fn=collect_local_repo_context_fn or collect_local_repo_context,
    )


def ask_board_message(
    *,
    session: Session,
    tenant,
    user_id: str,
    channel_id: str,
    question: str,
    scoped_issue_key: str | None = None,
    scoped_project_id: str | None = None,
    answer_persona_id: str | None = None,
    get_settings_fn=None,
    resolve_codex_working_dir_fn=None,
    resolve_scoped_secret_ref_fn=None,
    resolve_platform_secret_ref_fn=None,
    github_client_from_tenant_config_fn=None,
    collect_local_repo_context_fn=None,
) -> tuple[str, dict]:
    get_settings_impl = get_settings_fn or get_settings
    resolve_codex_working_dir_impl = resolve_codex_working_dir_fn or resolve_codex_working_dir
    resolve_scoped_secret_ref_impl = resolve_scoped_secret_ref_fn or resolve_scoped_secret_ref
    resolve_platform_secret_ref_impl = resolve_platform_secret_ref_fn or resolve_platform_secret_ref
    github_client_from_tenant_config_impl = (
        github_client_from_tenant_config_fn or github_client_from_tenant_config
    )
    collect_local_repo_context_impl = collect_local_repo_context_fn or collect_local_repo_context
    return _ask_board_message_impl(
        session=session,
        tenant=tenant,
        user_id=user_id,
        channel_id=channel_id,
        question=question,
        scoped_issue_key=scoped_issue_key,
        scoped_project_id=scoped_project_id,
        answer_persona_id=answer_persona_id,
        prune_missing_issue_keys_from_ask_history_fn=ask_history_runtime.prune_missing_issue_keys_from_ask_history,
        recent_ask_history_fn=ask_history_runtime.recent_ask_history,
        collect_ask_context_with_history_context_fn=lambda **kwargs: collect_ask_context_with_history_context(
            **kwargs,
            prune_history=False,
        ),
        get_settings_fn=get_settings_impl,
        build_codex_runtime_fn=build_codex_runtime,
        tenant_project_keys_fn=_tenant_project_keys,
        resolve_codex_working_dir_fn=resolve_codex_working_dir_impl,
        normalize_scope_channel_id_fn=normalize_scope_channel_id,
        channel_scope_repository=_channel_scope_repository,
        answer_board_question_with_runtime_fn=answer_board_question_with_runtime,
        collect_github_ask_context_fn=lambda **kwargs: collect_github_ask_context(
            **kwargs,
            get_settings_fn=get_settings_impl,
            resolve_scoped_secret_ref_fn=resolve_scoped_secret_ref_impl,
            resolve_platform_secret_ref_fn=resolve_platform_secret_ref_impl,
            github_client_from_tenant_config_fn=github_client_from_tenant_config_impl,
            collect_local_repo_context_fn=collect_local_repo_context_impl,
        ),
        codex_runtime_error_type=CodexRuntimeError,
        store_ask_history_entry_fn=ask_history_runtime.store_ask_history_entry,
    )
