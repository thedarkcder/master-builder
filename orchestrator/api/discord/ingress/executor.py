from __future__ import annotations

import re
from typing import Literal

from sqlalchemy.orm import Session

from orchestrator.api.commands.executor_registry import register_tenant_command_executor
from orchestrator.api.discord.ask.context import (
    tenant_project_keys as _tenant_project_keys,
)
from orchestrator.api.discord.ingress import (
    ask_history_runtime,
    ask_runtime,
    bug_runtime,
    gap_runtime,
    jira_runtime,
)
from orchestrator.api.discord.ingress.service import (
    execute_tenant_command_ingress as _execute_tenant_command_ingress,
)
from orchestrator.api.discord.ingress.wiring import build_discord_ingress_dependencies
from orchestrator.api.discord.shared.channel_scope_repository import (
    SqlAlchemyDiscordChannelScopeRepository,
)
from orchestrator.api.discord.shared.execution_policy import (
    ensure_issue_is_executable as _ensure_issue_is_executable_impl,
)
from orchestrator.api.discord.shared.scope_service import (
    enrich_command_scope as _enrich_command_scope_impl,
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
from orchestrator.core.runtime.runtime import build_codex_runtime
from orchestrator.core.runtime.working_dir import (
    resolve_codex_working_dir as _resolve_codex_working_dir_impl,
)
from orchestrator.core.communications.command_pipeline import CommandScope
from orchestrator.core.config import get_settings
from orchestrator.core.decision.clarification_port import (
    DecisionClarificationPort,
    RuntimeDecisionClarificationPort,
)
from orchestrator.core.projects.routing import find_active_project_for_issue_key
from orchestrator.core.runs.service import (
    RUN_STATUS_BLOCKED,
    RUN_STATUS_CANCELLED,
    RUN_STATUS_FAILED,
)
from orchestrator.core.platform.secret_service import resolve_platform_secret_ref
from orchestrator.core.platform.tenant_secret_service import resolve_scoped_secret_ref
from orchestrator.runtime import issue_fanout
from orchestrator.storage.models import Project, Tenant
from orchestrator.tools.github_app import github_client_from_tenant_config
from orchestrator.tools.project_repo_checkout import collect_local_repo_context

_channel_scope_repository = SqlAlchemyDiscordChannelScopeRepository()

RETRYABLE_STATUSES = {RUN_STATUS_FAILED, RUN_STATUS_BLOCKED, RUN_STATUS_CANCELLED}
ISSUE_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")
_default_decision_clarification_port: DecisionClarificationPort = (
    RuntimeDecisionClarificationPort()
)


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


def _resolve_command_scope(
    *, session: Session, tenant: Tenant, channel_id: str | None
) -> CommandScope:
    return _resolve_command_scope_impl(
        session=session,
        tenant=tenant,
        channel_id=channel_id,
        channel_scope_repository=_channel_scope_repository,
        tenant_project_keys_fn=_tenant_project_keys,
    )


def _enrich_command_scope(
    *,
    session: Session,
    tenant: Tenant,
    command_name: str,
    arguments: tuple[str, ...],
    payload,
    current_scope: CommandScope,
) -> CommandScope:  # noqa: ANN001
    return _enrich_command_scope_impl(
        session=session,
        tenant=tenant,
        command_name=command_name,
        arguments=arguments,
        payload=payload,
        current_scope=current_scope,
        find_active_project_for_issue_key_fn=find_active_project_for_issue_key,
    )


def _resolve_project_for_issue(
    *, session: Session, tenant: Tenant, issue_key: str
) -> Project:
    return _resolve_project_for_issue_impl(
        session=session,
        tenant=tenant,
        issue_key=issue_key,
        find_active_project_for_issue_key_fn=find_active_project_for_issue_key,
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
        decision_clarification_port=_default_decision_clarification_port,
        get_tenant_fn=lambda db, current_tenant_id: db.get(Tenant, current_tenant_id),
        assert_channel_scope_fn=lambda db, current_tenant, channel_id: (
            _assert_channel_scope(
                session=db,
                tenant=current_tenant,
                channel_id=channel_id,
            )
        ),
        assert_sensitive_command_permission_fn=lambda db, current_tenant, command_name, user_id, channel_id: (
            _assert_sensitive_command_permission(
                session=db,
                tenant=current_tenant,
                command_name=command_name,
                user_id=user_id,
                channel_id=channel_id,
            )
        ),
        resolve_scope_fn=lambda db, current_tenant, channel_id: _resolve_command_scope(
            session=db,
            tenant=current_tenant,
            channel_id=channel_id,
        ),
        enrich_scope_fn=lambda db, current_tenant, command_name, arguments, payload, current_scope: (
            _enrich_command_scope(
                session=db,
                tenant=current_tenant,
                command_name=command_name,
                arguments=arguments,
                payload=payload,
                current_scope=current_scope,
            )
        ),
        prune_missing_issue_keys_from_ask_history_fn=ask_history_runtime.prune_missing_issue_keys_from_ask_history,
        recent_ask_history_fn=ask_history_runtime.recent_ask_history,
        collect_ask_context_with_history_context_fn=ask_runtime.collect_ask_context_with_history_context,
        collect_github_ask_context_fn=lambda **kwargs: (
            ask_runtime.collect_github_ask_context(
                **kwargs,
                get_settings_fn=get_settings,
                resolve_scoped_secret_ref_fn=resolve_scoped_secret_ref,
                resolve_platform_secret_ref_fn=resolve_platform_secret_ref,
                github_client_from_tenant_config_fn=github_client_from_tenant_config,
                collect_local_repo_context_fn=collect_local_repo_context,
            )
        ),
        store_pending_ask_action_fn=ask_history_runtime.store_pending_ask_action,
        store_ask_history_entry_fn=ask_history_runtime.store_ask_history_entry,
        ask_board_message_fn=lambda **kwargs: ask_runtime.ask_board_message(
            **kwargs,
            get_settings_fn=get_settings,
            resolve_codex_working_dir_fn=_resolve_codex_working_dir,
            resolve_scoped_secret_ref_fn=resolve_scoped_secret_ref,
            resolve_platform_secret_ref_fn=resolve_platform_secret_ref,
            github_client_from_tenant_config_fn=github_client_from_tenant_config,
            collect_local_repo_context_fn=collect_local_repo_context,
        ),
        run_gap_analysis_fn=gap_runtime.run_gap_analysis,
        normalize_discord_attachments_fn=bug_runtime.normalize_discord_attachments,
        create_discord_bug_issue_fn=bug_runtime.create_discord_bug_issue,
        seed_parent_issues_with_runtime_fn=issue_fanout.seed_parent_issues_with_runtime,
        seed_issues_with_runtime_fn=issue_fanout.seed_issues_with_runtime,
        find_seed_followup_context_fn=_find_seed_followup_context,
        store_seed_followup_context_fn=_store_seed_followup_context,
        clear_seed_followup_context_fn=_clear_seed_followup_context,
        validate_seed_followup_context_fn=issue_fanout.validate_seed_followup_context,
        resolve_project_for_issue_fn=_resolve_project_for_issue,
        fetch_issue_preview_fn=jira_runtime.fetch_jira_issue_preview,
        fetch_issue_detail_fn=jira_runtime.fetch_jira_issue_detail,
        settings_factory_fn=get_settings,
        build_codex_runtime_fn=build_codex_runtime,
        tenant_atlassian_oauth_context_fn=jira_runtime.tenant_atlassian_oauth_context,
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
    ingress_source: Literal["discord", "jira_comment"] = "discord",
) -> DiscordCommandResponse:
    return _execute_tenant_command_ingress(
        tenant_id=tenant_id,
        payload=payload,
        session=session,
        defer_seed_issues=defer_seed_issues,
        require_ask_confirmation=require_ask_confirmation,
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
    ingress_source: Literal["discord", "jira_comment"] = "discord",
) -> DiscordCommandResponse:
    return execute_tenant_command_ingress(
        tenant_id=tenant_id,
        payload=payload,
        session=session,
        defer_seed_issues=defer_seed_issues,
        require_ask_confirmation=require_ask_confirmation,
        ingress_source=ingress_source,
    )


def register_discord_command_executor() -> None:
    register_tenant_command_executor(execute_tenant_command_ingress)
