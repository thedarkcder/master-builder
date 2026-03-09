from __future__ import annotations

import re
from typing import Literal

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.api.dependencies import get_session
from orchestrator.api.discord.ingress.service import (
    execute_tenant_command_ingress as _execute_tenant_command_ingress,
)
from orchestrator.api.discord.ingress.wiring import build_discord_ingress_dependencies
from orchestrator.api.discord.ask.context import (
    fetch_jira_issue_detail_for_tenant,
    fetch_jira_issue_preview_for_tenant,
    project_filter_jql as _project_filter_jql,
    search_jira_issues_for_tenant as _search_jira_issues_for_tenant,
    tenant_project_keys as _tenant_project_keys,
)
from orchestrator.api.discord.shared.channel_scope_repository import SqlAlchemyDiscordChannelScopeRepository
from orchestrator.api.discord.bug.attachments import (
    AttachmentUploadFailure,
    download_discord_attachment as _download_discord_attachment_impl,
    resolve_discord_channel_name as _resolve_discord_channel_name_impl,
    upload_discord_attachments_to_jira as _upload_discord_attachments_to_jira_impl,
)
from orchestrator.api.discord.bug.issue_create_service import (
    create_discord_bug_issue as _create_discord_bug_issue_impl,
)
from orchestrator.api.discord.bug.service import build_discord_bug_description, normalize_discord_attachments
from orchestrator.api.discord.bug.gap_analysis import (
    extract_acceptance_criteria_from_description as _extract_acceptance_criteria_from_description_impl,
    gap_confidence as _gap_confidence_impl,
    run_gap_analysis as _run_gap_analysis_impl,
    tenant_repo_url as _tenant_repo_url_impl,
)
from orchestrator.api.discord.ask.query_service import (
    collect_ask_context as _collect_ask_context_impl,
)
from orchestrator.api.discord.seed.matching import (
    select_seed_match as _select_seed_match_impl,
)
from orchestrator.api.discord.seed.description import (
    build_seed_issue_description as _build_seed_issue_description_impl,
)
from orchestrator.api.discord.shared.scope_service import (
    normalize_scope_channel_id as _normalize_scope_channel_id_impl,
    resolve_command_scope as _resolve_command_scope_impl,
    resolve_project_for_issue as _resolve_project_for_issue_impl,
)
from orchestrator.api.discord.shared.execution_policy import (
    ensure_issue_is_executable as _ensure_issue_is_executable_impl,
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
from orchestrator.api.discord.ask.board_service import ask_board_message as _ask_board_message_impl
from orchestrator.api.discord.seed.issue_service import (
    seed_issues_with_codex as _seed_issues_with_codex_impl,
    validate_seed_followup_context as _validate_seed_followup_context_impl,
)
from orchestrator.api.jira_oauth.connection_service import resolve_tenant_jira_connection
from orchestrator.api.commands.executor_registry import register_tenant_command_executor
from orchestrator.api.discord.shared.state import (
    assert_channel_scope as _assert_channel_scope,
    assert_sensitive_command_permission as _assert_sensitive_command_permission,
    clear_seed_followup_context as _clear_seed_followup_context,
    find_seed_followup_context as _find_seed_followup_context,
    normalize_status_name as _normalize_status_name,
    store_seed_followup_context as _store_seed_followup_context,
)
from orchestrator.api.jira_oauth.service import jira_oauth_client as _jira_oauth_client
from orchestrator.api.jira_oauth.service import refresh_jira_connection_tokens as _refresh_jira_connection_tokens
from orchestrator.api.schemas import DiscordCommandRequest, DiscordCommandResponse
from orchestrator.core.codex_agents import (
    answer_board_question_with_codex,
    plan_seed_issues_with_codex,
)
from orchestrator.core.codex_working_dir import resolve_codex_working_dir as _resolve_codex_working_dir_impl
from orchestrator.core.communications.command_pipeline import (
    CommandScope,
)
from orchestrator.core.codex_runtime import CodexRuntimeError, build_codex_runtime
from orchestrator.core.config import get_settings
from orchestrator.core.platform_secret_service import (
    PLATFORM_SECRET_DISCORD_BOT_TOKEN_REF,
    resolve_platform_secret_ref,
)
from orchestrator.core.tenant_secret_service import resolve_scoped_secret_ref
from orchestrator.core.project_routing import find_active_project_for_issue_key
from orchestrator.core.runs import (
    RUN_STATUS_BLOCKED,
    RUN_STATUS_CANCELLED,
    RUN_STATUS_FAILED,
)
from orchestrator.storage.models import Project, Tenant
from orchestrator.tools.github_app import GitHubApiError, github_client_from_tenant_config
from orchestrator.tools.project_repo_checkout import collect_local_repo_context
from orchestrator.tools.jira_oauth import JiraIssuePreview
from orchestrator.tools.repo_allowlist import normalize_repo_identifier

router = APIRouter(tags=["discord"])
_channel_scope_repository = SqlAlchemyDiscordChannelScopeRepository()

RETRYABLE_STATUSES = {RUN_STATUS_FAILED, RUN_STATUS_BLOCKED, RUN_STATUS_CANCELLED}
ISSUE_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")
MAX_ASK_HISTORY_CONTEXT = ASK_HISTORY_CONTEXT_LIMIT
def _normalize_scope_channel_id(channel_id: str | None) -> str | None:
    return _normalize_scope_channel_id_impl(channel_id)


def _tenant_repo_url(tenant: Tenant) -> str | None:
    return _tenant_repo_url_impl(tenant)


def _tenant_jira_oauth_context(*, session: Session, tenant: Tenant, settings):  # noqa: ANN001
    connection = resolve_tenant_jira_connection(session=session, tenant=tenant)
    access_token = _refresh_jira_connection_tokens(
        session,
        connection=connection,
        settings=settings,
    )
    client = _jira_oauth_client(session=session, settings=settings)
    return {"connection": connection, "access_token": access_token, "client": client}


def _fetch_jira_issue_preview(*, session: Session, tenant: Tenant, issue_key: str) -> JiraIssuePreview:
    return fetch_jira_issue_preview_for_tenant(
        session=session,
        tenant=tenant,
        issue_key=issue_key,
    )


def _fetch_jira_issue_detail(*, session: Session, tenant: Tenant, issue_key: str):  # noqa: ANN001
    return fetch_jira_issue_detail_for_tenant(
        session=session,
        tenant=tenant,
        issue_key=issue_key,
    )


def _extract_acceptance_criteria_from_description(description: str) -> list[str]:
    return _extract_acceptance_criteria_from_description_impl(description)


def _gap_confidence(*, has_acceptance: bool, has_successful_run: bool, has_pr: bool) -> str:
    return _gap_confidence_impl(
        has_acceptance=has_acceptance,
        has_successful_run=has_successful_run,
        has_pr=has_pr,
    )


def _run_gap_analysis(
    *,
    session: Session,
    tenant: Tenant,
    issue_key: str,
) -> tuple[str, dict]:
    return _run_gap_analysis_impl(
        session=session,
        tenant=tenant,
        issue_key=issue_key,
        issue_key_pattern=ISSUE_KEY_PATTERN,
        collect_project_repo_context_fn=_collect_project_repo_context_for_issue,
    )


def _collect_project_repo_context_for_issue(
    *,
    session: Session,
    tenant: Tenant,
    issue_key: str,
) -> dict:
    project = find_active_project_for_issue_key(
        session,
        tenant_id=tenant.tenant_id,
        issue_key=issue_key,
    )
    if project is None:
        return {"available": False, "reason": "project_not_mapped"}
    settings = get_settings()
    local_context = collect_local_repo_context(
        base_dir=settings.project_repo_checkout_base_dir,
        tenant_id=tenant.tenant_id,
        project=project,
        issue_key=issue_key,
    )
    return {
        "available": local_context.available,
        "reason": local_context.reason,
        "repo_dir": local_context.repo_dir,
        "current_branch": local_context.current_branch,
        "head_sha": local_context.head_sha,
        "branches": local_context.branches,
        "recent_commits": local_context.recent_commits,
        "issue_related_commits": local_context.issue_related_commits,
    }


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


def _resolve_project_for_issue(
    *,
    session: Session,
    tenant: Tenant,
    issue_key: str,
) -> Project:
    return _resolve_project_for_issue_impl(
        session=session,
        tenant=tenant,
        issue_key=issue_key,
        find_active_project_for_issue_key_fn=find_active_project_for_issue_key,
    )


def _select_seed_match(
    *,
    existing_issues: list[JiraIssuePreview],
    summary: str,
    requested_issue_key: str | None,
    matched_issue_keys: set[str],
) -> JiraIssuePreview | None:
    return _select_seed_match_impl(
        existing_issues=existing_issues,
        summary=summary,
        requested_issue_key=requested_issue_key,
        matched_issue_keys=matched_issue_keys,
    )


def _build_seed_issue_description(
    *,
    objective: str,
    scope_in: list[str],
    scope_out: list[str],
    acceptance_criteria: list[str],
    how_to_test: list[str],
    nfr_intent: str,
    dependencies_and_risks: list[str],
) -> dict:
    return _build_seed_issue_description_impl(
        objective=objective,
        scope_in=scope_in,
        scope_out=scope_out,
        acceptance_criteria=acceptance_criteria,
        how_to_test=how_to_test,
        nfr_intent=nfr_intent,
        dependencies_and_risks=dependencies_and_risks,
    )


def _normalize_discord_attachments(raw_attachments: object) -> list[dict[str, str]]:
    return normalize_discord_attachments(raw_attachments)


def _build_discord_bug_description(
    *,
    summary: str,
    details: str,
    reporter_user_id: str,
    channel_id: str | None,
    related_issue_key: str | None,
    attachments: list[dict[str, str]],
) -> str:
    return build_discord_bug_description(
        summary=summary,
        details=details,
        reporter_user_id=reporter_user_id,
        channel_id=channel_id,
        related_issue_key=related_issue_key,
        attachments=attachments,
    )


def _resolve_discord_channel_name(
    *,
    session: Session,
    tenant: Tenant,
    channel_id: str | None,
) -> str | None:
    settings = get_settings()
    return _resolve_discord_channel_name_impl(
        session=session,
        tenant=tenant,
        channel_id=channel_id,
        discord_bot_token_secret_ref=PLATFORM_SECRET_DISCORD_BOT_TOKEN_REF,
        secrets_encryption_key=settings.secrets_encryption_key,
    )


def _download_discord_attachment(*, url: str, bot_token: str | None = None) -> tuple[bytes, str | None]:
    return _download_discord_attachment_impl(url=url, bot_token=bot_token)


def _create_discord_bug_issue(
    *,
    session: Session,
    tenant: Tenant,
    summary: str,
    details: str,
    reporter_user_id: str,
    channel_id: str | None,
    related_issue_key: str | None,
    attachments: list[dict[str, str]],
    selected_project_key: str | None = None,
) -> tuple[str, dict]:
    settings = get_settings()
    bot_token = resolve_platform_secret_ref(
        session,
        secret_ref=PLATFORM_SECRET_DISCORD_BOT_TOKEN_REF,
        encryption_key=settings.secrets_encryption_key,
    )

    def _download_attachment(*, url: str) -> tuple[bytes, str | None]:
        return _download_discord_attachment(url=url, bot_token=bot_token)

    def _upload_attachments(
        *,
        client,
        access_token: str,
        cloud_id: str,
        issue_key: str,
        attachments: list[dict[str, str]],
        correlation_id: str | None = None,
    ) -> tuple[int, list[AttachmentUploadFailure]]:
        return _upload_discord_attachments_to_jira_impl(
            client=client,
            access_token=access_token,
            cloud_id=cloud_id,
            issue_key=issue_key,
            attachments=attachments,
            correlation_id=correlation_id,
            download_attachment=_download_attachment,
        )

    return _create_discord_bug_issue_impl(
        session=session,
        tenant=tenant,
        summary=summary,
        details=details,
        reporter_user_id=reporter_user_id,
        channel_id=channel_id,
        related_issue_key=related_issue_key,
        attachments=attachments,
        selected_project_key=selected_project_key,
        tenant_project_keys_fn=_tenant_project_keys,
        resolve_discord_channel_name_fn=_resolve_discord_channel_name,
        tenant_jira_oauth_context_fn=_tenant_jira_oauth_context,
        upload_discord_attachments_to_jira_fn=_upload_attachments,
        settings=settings,
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


def _repo_full_name_from_repository_url(repository_url: str) -> str | None:
    normalized_repo = normalize_repo_identifier(repository_url)
    github_prefix = "github.com/"
    if not normalized_repo.startswith(github_prefix):
        return None
    repo_full_name = normalized_repo[len(github_prefix):].strip("/")
    if repo_full_name.count("/") != 1:
        return None
    return repo_full_name


def _collect_github_ask_context(
    *,
    session: Session,
    tenant: Tenant,
    project_keys: list[str],
) -> dict:
    normalized_project_keys = {str(value).strip().upper() for value in project_keys if str(value).strip()}
    query = select(Project).where(
        Project.tenant_id == tenant.tenant_id,
        Project.is_archived.is_(False),
    )
    projects = session.execute(query).scalars().all()
    scoped_projects = [
        project
        for project in projects
        if not normalized_project_keys or project.jira_project_key.strip().upper() in normalized_project_keys
    ]
    if not scoped_projects:
        return {"available": False, "reason": "no_active_projects", "repositories": []}

    repo_map: dict[str, dict] = {}
    for project in scoped_projects:
        repo_full_name = _repo_full_name_from_repository_url(project.github_repository)
        if not repo_full_name:
            continue
        entry = repo_map.setdefault(
            repo_full_name,
            {
                "repo_full_name": repo_full_name,
                "project_keys": [],
                "open_pull_requests": [],
                "staging_pull_requests": [],
                "local_repo": {},
            },
        )
        project_key = project.jira_project_key.strip().upper()
        if project_key and project_key not in entry["project_keys"]:
            entry["project_keys"].append(project_key)

    if not repo_map:
        return {"available": False, "reason": "no_github_repositories", "repositories": []}

    settings = get_settings()
    for project in scoped_projects:
        repo_full_name = _repo_full_name_from_repository_url(project.github_repository)
        if not repo_full_name or repo_full_name not in repo_map:
            continue
        local_context = collect_local_repo_context(
            base_dir=settings.project_repo_checkout_base_dir,
            tenant_id=tenant.tenant_id,
            project=project,
        )
        repo_map[repo_full_name]["local_repo"] = {
            "available": local_context.available,
            "reason": local_context.reason,
            "repo_dir": local_context.repo_dir,
            "current_branch": local_context.current_branch,
            "head_sha": local_context.head_sha,
            "branches": local_context.branches,
            "recent_commits": local_context.recent_commits,
        }

    try:
        github_client = github_client_from_tenant_config(
            tenant.github_config,
            tenant_secret_lookup=lambda secret_ref: resolve_scoped_secret_ref(
                session,
                secret_ref=secret_ref,
                encryption_key=settings.secrets_encryption_key,
                tenant_id=tenant.tenant_id,
            ),
            platform_secret_lookup=lambda secret_ref: resolve_platform_secret_ref(
                session,
                secret_ref=secret_ref,
                encryption_key=settings.secrets_encryption_key,
            ),
        )
    except ValueError as exc:
        return {
            "available": False,
            "reason": f"github_client_unavailable:{exc}",
            "repositories": list(repo_map.values()),
        }

    repo_contexts: list[dict] = []
    degraded_repositories: list[str] = []
    for repo_full_name in sorted(repo_map.keys())[:5]:
        repo_entry = repo_map[repo_full_name]
        try:
            pull_requests = github_client.list_open_pull_requests(
                repo_full_name=repo_full_name,
                limit=15,
            )
        except (GitHubApiError, ValueError):
            pull_requests = []
            degraded_repositories.append(repo_full_name)
        repo_entry["open_pull_requests"] = [
            {
                "number": pr.number,
                "title": pr.title,
                "state": pr.state,
                "head_ref": pr.head_ref,
                "base_ref": pr.base_ref,
                "html_url": pr.html_url,
                "updated_at": pr.updated_at,
            }
            for pr in pull_requests
        ]
        repo_entry["staging_pull_requests"] = [
            pr_entry
            for pr_entry in repo_entry["open_pull_requests"]
            if str(pr_entry.get("base_ref") or "").strip().lower() == "staging"
        ]
        repo_contexts.append(repo_entry)
    if degraded_repositories:
        return {
            "available": False,
            "reason": "github_pull_requests_unavailable",
            "degraded_repositories": degraded_repositories,
            "repositories": repo_contexts,
        }
    return {"available": True, "repositories": repo_contexts}


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


def consume_pending_ask_action(
    *,
    session: Session,
    tenant: Tenant,
    request_id: str,
) -> dict | None:
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


def _seed_issues_with_codex(
    *,
    session: Session,
    tenant: Tenant,
    prompt_markdown: str,
    force_issue_keys: list[str] | None = None,
    allow_create: bool = True,
    scoped_project_keys: list[str] | None = None,
    codex_working_dir: str = "",
) -> tuple[str, dict]:
    return _seed_issues_with_codex_impl(
        session=session,
        tenant=tenant,
        prompt_markdown=prompt_markdown,
        force_issue_keys=force_issue_keys,
        allow_create=allow_create,
        scoped_project_keys=scoped_project_keys,
        codex_working_dir=codex_working_dir,
        tenant_project_keys_fn=_tenant_project_keys,
        get_settings_fn=get_settings,
        build_codex_runtime_fn=build_codex_runtime,
        plan_seed_issues_with_codex_fn=plan_seed_issues_with_codex,
        codex_runtime_error_type=CodexRuntimeError,
        build_seed_issue_description_fn=_build_seed_issue_description,
        issue_key_pattern=ISSUE_KEY_PATTERN,
        tenant_jira_oauth_context_fn=_tenant_jira_oauth_context,
        select_seed_match_fn=_select_seed_match,
    )


def _validate_seed_followup_context(
    *,
    session: Session,
    tenant: Tenant,
    context: dict,
) -> tuple[bool, str | None]:
    return _validate_seed_followup_context_impl(
        session=session,
        tenant=tenant,
        context=context,
        get_settings_fn=get_settings,
        tenant_jira_oauth_context_fn=_tenant_jira_oauth_context,
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


def execute_tenant_command_ingress(
    tenant_id: str,
    payload: DiscordCommandRequest,
    session: Session = Depends(get_session),
    *,
    defer_seed_issues: bool = False,
    require_ask_confirmation: bool = False,
    allow_plain_ask: bool = False,
    ingress_source: Literal["discord", "jira_comment"] = "discord",
) -> DiscordCommandResponse:
    deps = build_discord_ingress_dependencies(
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
        run_gap_analysis_fn=_run_gap_analysis,
        normalize_discord_attachments_fn=_normalize_discord_attachments,
        create_discord_bug_issue_fn=_create_discord_bug_issue,
        seed_issues_with_codex_fn=_seed_issues_with_codex,
        find_seed_followup_context_fn=_find_seed_followup_context,
        store_seed_followup_context_fn=_store_seed_followup_context,
        clear_seed_followup_context_fn=_clear_seed_followup_context,
        validate_seed_followup_context_fn=_validate_seed_followup_context,
        resolve_project_for_issue_fn=_resolve_project_for_issue,
        fetch_issue_preview_fn=_fetch_jira_issue_preview,
        fetch_issue_detail_fn=_fetch_jira_issue_detail,
        settings_factory_fn=get_settings,
        build_codex_runtime_fn=build_codex_runtime,
        tenant_jira_oauth_context_fn=_tenant_jira_oauth_context,
        ensure_issue_is_executable_fn=_ensure_issue_is_executable,
        resolve_codex_working_dir_fn=_resolve_codex_working_dir,
    )
    return _execute_tenant_command_ingress(
        tenant_id=tenant_id,
        payload=payload,
        session=session,
        defer_seed_issues=defer_seed_issues,
        require_ask_confirmation=require_ask_confirmation,
        allow_plain_ask=allow_plain_ask,
        ingress_source=ingress_source,
        deps=deps,
    )


def execute_discord_command(
    tenant_id: str,
    payload: DiscordCommandRequest,
    session: Session = Depends(get_session),
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


@router.post("/discord/command/{tenant_id}", response_model=DiscordCommandResponse)
def execute_discord_command_route(
    tenant_id: str,
    payload: DiscordCommandRequest,
    session: Session = Depends(get_session),
    *,
    defer_seed_issues: bool = False,
    require_ask_confirmation: bool = False,
    allow_plain_ask: bool = False,
) -> DiscordCommandResponse:
    return execute_discord_command(
        tenant_id=tenant_id,
        payload=payload,
        session=session,
        defer_seed_issues=defer_seed_issues,
        require_ask_confirmation=require_ask_confirmation,
        allow_plain_ask=allow_plain_ask,
        ingress_source="discord",
    )


def register_discord_command_executor() -> None:
    register_tenant_command_executor(execute_tenant_command_ingress)


register_discord_command_executor()
