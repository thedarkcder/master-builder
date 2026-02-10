from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.api.dependencies import get_session
from orchestrator.api.discord_ingress_service import (
    DiscordIngressDependencies,
    DiscordIngressHandlers,
    execute_tenant_command_ingress as _execute_tenant_command_ingress,
)
from orchestrator.api.discord_command_dispatcher import dispatch_simple_discord_command
from orchestrator.api.discord_command_bug_gap import dispatch_bug_gap_command
from orchestrator.api.discord_command_issues import dispatch_issues_command
from orchestrator.api.discord_command_parser import resolve_discord_command
from orchestrator.api.discord_command_run_controls import dispatch_run_control_command
from orchestrator.api.discord_command_ask import dispatch_ask_command
from orchestrator.api.discord_ask_context import (
    fetch_jira_issue_preview_for_tenant,
    project_filter_jql as _project_filter_jql,
    search_jira_issues_for_tenant as _search_jira_issues_for_tenant,
    tenant_project_keys as _tenant_project_keys,
)
from orchestrator.api.discord_channel_scope_repository import SqlAlchemyDiscordChannelScopeRepository
from orchestrator.api.discord_bug_service import build_discord_bug_description, normalize_discord_attachments
from orchestrator.api.discord_gap_analysis import (
    extract_acceptance_criteria_from_description as _extract_acceptance_criteria_from_description_impl,
    gap_confidence as _gap_confidence_impl,
    run_gap_analysis as _run_gap_analysis_impl,
    tenant_repo_url as _tenant_repo_url_impl,
)
from orchestrator.api.discord_seed_normalization import (
    collect_seed_issue_questions as _collect_seed_issue_questions_impl,
    normalize_seed_issue_key as _normalize_seed_issue_key_impl,
    normalize_seed_issue_labels as _normalize_seed_issue_labels_impl,
    normalize_seed_issue_scope as _normalize_seed_issue_scope_impl,
    normalize_seed_issue_tags as _normalize_seed_issue_tags_impl,
    parse_seed_issue_type as _parse_seed_issue_type_impl,
    seed_text_is_missing as _seed_text_is_missing_impl,
)
from orchestrator.api.discord_seed_matching import (
    normalized_summary_key as _normalized_summary_key_impl,
    select_seed_match as _select_seed_match_impl,
    summary_similarity as _summary_similarity_impl,
)
from orchestrator.api.discord_ask_memory import (
    MAX_ASK_HISTORY_CONTEXT as ASK_HISTORY_CONTEXT_LIMIT,
    collect_ask_context_with_history_context as _collect_ask_context_with_history_context_impl,
    consume_pending_ask_action as _consume_pending_ask_action_impl,
    drop_issue_key_from_ask_history as _drop_issue_key_from_ask_history_impl,
    existing_issue_keys_for_tenant as _existing_issue_keys_for_tenant_impl,
    prune_missing_issue_keys_from_ask_history as _prune_missing_issue_keys_from_ask_history_impl,
    recent_ask_history as _recent_ask_history_impl,
    remove_issue_key_from_tenant_ask_history,
    store_ask_history_entry as _store_ask_history_entry_impl,
    store_pending_ask_action as _store_pending_ask_action_impl,
    tenant_ask_history as _tenant_ask_history_impl,
)
from orchestrator.api.discord_response_format import (
    build_issue_url_list,
    build_jira_issue_url,
    format_issue_markdown_list,
)
from orchestrator.api.discord_followup_format import resolve_tenant_jira_browse_base_url
from orchestrator.api.jira_oauth_connection_service import resolve_tenant_jira_connection
from orchestrator.api.command_executor_registry import register_tenant_command_executor
from orchestrator.api.discord_state import (
    assert_channel_scope as _assert_channel_scope,
    assert_sensitive_command_permission as _assert_sensitive_command_permission,
    clear_seed_followup_context as _clear_seed_followup_context,
    find_seed_followup_context as _find_seed_followup_context,
    normalize_status_name as _normalize_status_name,
    store_seed_followup_context as _store_seed_followup_context,
)
from orchestrator.api.jira_oauth_service import jira_oauth_client as _jira_oauth_client
from orchestrator.api.jira_oauth_service import refresh_jira_connection_tokens as _refresh_jira_connection_tokens
from orchestrator.api.schemas import DiscordCommandRequest, DiscordCommandResponse
from orchestrator.core.codex_agents import (
    answer_board_question_with_codex,
    plan_seed_issues_with_codex,
)
from orchestrator.core.communications.command_pipeline import (
    CommandScope,
)
from orchestrator.core.codex_runtime import CodexRuntimeError, build_codex_runtime
from orchestrator.core.config import get_settings
from orchestrator.core.project_routing import find_active_project_for_issue_key
from orchestrator.core.secret_manager import resolve_scoped_secret_ref
from orchestrator.core.runs import (
    RUN_STATUS_BLOCKED,
    RUN_STATUS_CANCELLED,
    RUN_STATUS_FAILED,
    RUN_STATUS_SUCCEEDED,
)
from orchestrator.storage.models import Project, Run, Tenant
from orchestrator.tools.discord_api import DiscordApiClient, DiscordApiError
from orchestrator.tools.jira_oauth import JiraIssueCreateInput, JiraIssuePreview, JiraOAuthError

router = APIRouter(tags=["discord"])
_channel_scope_repository = SqlAlchemyDiscordChannelScopeRepository()

RETRYABLE_STATUSES = {RUN_STATUS_FAILED, RUN_STATUS_BLOCKED, RUN_STATUS_CANCELLED}
ISSUE_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")
MAX_ASK_HISTORY_CONTEXT = ASK_HISTORY_CONTEXT_LIMIT
DM_SCOPE_SENTINEL_CHANNEL_IDS = {"__dm__", "__dm", "dm"}
def _normalize_scope_channel_id(channel_id: str | None) -> str | None:
    normalized_channel_id = str(channel_id or "").strip()
    if not normalized_channel_id:
        return None
    if normalized_channel_id.lower() in DM_SCOPE_SENTINEL_CHANNEL_IDS:
        return None
    return normalized_channel_id


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
    )


def _ensure_issue_is_executable(*, issue_status: str, tenant: Tenant) -> None:
    executable_statuses = ["To Do"]
    configured_ready_statuses = tenant.jira_config.get("ready_statuses")
    if isinstance(configured_ready_statuses, list):
        executable_statuses.extend(
            status_name.strip()
            for status_name in (str(value) for value in configured_ready_statuses)
            if status_name.strip()
        )
    normalized_executable_statuses = {_normalize_status_name(value) for value in executable_statuses}
    if _normalize_status_name(issue_status) not in normalized_executable_statuses:
        display_statuses = ", ".join(sorted(set(executable_statuses)))
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Issue is in '{issue_status}', expected one of: {display_statuses}",
        )


def _resolve_command_scope(*, session: Session, tenant: Tenant, channel_id: str | None) -> CommandScope:
    normalized_channel_id = _normalize_scope_channel_id(channel_id)
    if normalized_channel_id:
        scope = _channel_scope_repository.resolve_project_scope(
            session=session,
            tenant=tenant,
            channel_id=normalized_channel_id,
        )
        if scope is not None and scope.jira_project_key:
            return CommandScope(
                project_id=scope.project_id,
                project_keys=(scope.jira_project_key,),
                channel_id=normalized_channel_id,
            )
    return CommandScope(
        project_keys=tuple(_tenant_project_keys(session=session, tenant=tenant)),
        channel_id=normalized_channel_id,
    )


def _resolve_project_for_issue(
    *,
    session: Session,
    tenant: Tenant,
    issue_key: str,
) -> Project:
    project = find_active_project_for_issue_key(
        session,
        tenant_id=tenant.tenant_id,
        issue_key=issue_key,
    )
    if project is not None:
        return project
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=f"No active project mapping found for issue {issue_key}",
    )


def _normalize_seed_issue_labels(raw_labels: object) -> list[str]:
    return _normalize_seed_issue_labels_impl(raw_labels)


def _normalize_seed_issue_tags(raw_tags: object) -> list[str]:
    return _normalize_seed_issue_tags_impl(raw_tags)


def _parse_seed_issue_type(raw_issue_type: object) -> str:
    return _parse_seed_issue_type_impl(raw_issue_type)


def _normalize_seed_issue_scope(raw_scope: object) -> list[str]:
    return _normalize_seed_issue_scope_impl(raw_scope)


def _normalize_seed_issue_key(raw_issue_key: object) -> str | None:
    return _normalize_seed_issue_key_impl(raw_issue_key, issue_key_pattern=ISSUE_KEY_PATTERN)


def _seed_text_is_missing(value: str) -> bool:
    return _seed_text_is_missing_impl(value)


def _collect_seed_issue_questions(*, issue_summary: str, objective: str, scope_in: list[str], scope_out: list[str], acceptance: list[str]) -> list[str]:
    return _collect_seed_issue_questions_impl(
        issue_summary=issue_summary,
        objective=objective,
        scope_in=scope_in,
        scope_out=scope_out,
        acceptance=acceptance,
    )


def _normalized_summary_key(summary: str) -> str:
    return _normalized_summary_key_impl(summary)


def _summary_similarity(left: str, right: str) -> float:
    return _summary_similarity_impl(left, right)


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
) -> dict:
    def _heading(text: str) -> dict:
        return {
            "type": "heading",
            "attrs": {"level": 3},
            "content": [{"type": "text", "text": text}],
        }

    def _bullet_list(items: list[str]) -> dict:
        return {
            "type": "bulletList",
            "content": [
                {
                    "type": "listItem",
                    "content": [{"type": "paragraph", "content": [{"type": "text", "text": item}]}],
                }
                for item in items
            ],
        }

    scope_in_items = scope_in if scope_in else ["Not specified"]
    scope_out_items = scope_out if scope_out else ["Not specified"]
    acceptance_items = acceptance_criteria if acceptance_criteria else ["Criteria were not provided"]

    content = [
        _heading("Objective"),
        _bullet_list([objective.strip() or "No objective provided"]),
        _heading("Scope In"),
        _bullet_list(scope_in_items),
        _heading("Scope Out"),
        _bullet_list(scope_out_items),
        _heading("Acceptance Criteria"),
        _bullet_list(acceptance_items),
        _heading("Good To Do Checklist"),
        _bullet_list(
            [
                "[ ] Objective is clear",
                "[ ] Scope is explicit (in/out)",
                "[ ] Acceptance criteria are testable",
                "[ ] How-to-test is defined",
                "[ ] MVP vs scale-ready is decided",
            ]
        ),
        _heading("Decision Gate Triggers"),
        _bullet_list(
            [
                "[ ] Requirements are ambiguous",
                "[ ] Design choice impacts NFRs/reliability/cost/security",
            ]
        ),
        _heading("Notes / Links"),
        _bullet_list(["Reported via Discord issue seeding flow"]),
    ]
    return {"type": "doc", "version": 1, "content": content}


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
    normalized_channel_id = str(channel_id or "").strip()
    if not normalized_channel_id:
        return None
    settings = get_settings()
    token_ref = settings.discord_bot_token_secret_ref.strip()
    if not token_ref:
        return None
    bot_token = resolve_scoped_secret_ref(
        session,
        secret_ref=token_ref,
        encryption_key=settings.secrets_encryption_key,
        tenant_id=tenant.tenant_id,
    )
    if not bot_token:
        return None
    try:
        client = DiscordApiClient(bot_token=bot_token)
        payload = client.get_channel(channel_id=normalized_channel_id)
    except (DiscordApiError, ValueError):
        return None
    name = str(payload.get("name") or "").strip()
    return name or None


def _download_discord_attachment(*, url: str) -> tuple[bytes, str | None]:
    request = Request(
        url=url,
        headers={"User-Agent": "MasterBuilderDiscordBugUploader/1.0"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=30) as response:
            payload = response.read()
            content_type = response.headers.get("Content-Type")
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise JiraOAuthError(f"HTTP {exc.code} downloading attachment: {body}") from exc
    except URLError as exc:
        raise JiraOAuthError(f"Failed to download attachment: {exc.reason}") from exc

    if not payload:
        raise JiraOAuthError("Downloaded attachment was empty")
    return payload, content_type.strip() if isinstance(content_type, str) and content_type.strip() else None


def _upload_discord_attachments_to_jira(
    *,
    client,
    access_token: str,
    cloud_id: str,
    issue_key: str,
    attachments: list[dict[str, str]],
) -> tuple[int, list[str]]:
    if not attachments:
        return 0, []

    uploaded_count = 0
    warnings: list[str] = []
    for attachment in attachments:
        filename = str(attachment.get("filename") or "").strip() or "attachment"
        url = str(attachment.get("url") or "").strip()
        if not url:
            warnings.append(f"{filename}: missing URL")
            continue
        try:
            content, downloaded_content_type = _download_discord_attachment(url=url)
            content_type = str(attachment.get("content_type") or "").strip() or downloaded_content_type
            client.upload_issue_attachment(
                access_token=access_token,
                cloud_id=cloud_id,
                issue_id_or_key=issue_key,
                filename=filename,
                content=content,
                content_type=content_type,
            )
            uploaded_count += 1
        except (JiraOAuthError, ValueError) as exc:
            warnings.append(f"{filename}: {exc}")
    return uploaded_count, warnings


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
) -> tuple[str, dict]:
    project_keys = _tenant_project_keys(session=session, tenant=tenant)
    if not project_keys:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Tenant has no Jira project keys")
    project_key = project_keys[0]
    channel_display_name = _resolve_discord_channel_name(
        session=session,
        tenant=tenant,
        channel_id=channel_id,
    )
    normalized_channel = (
        f"{channel_display_name} ({channel_id})" if channel_display_name and channel_id else channel_display_name or channel_id
    )

    description = _build_discord_bug_description(
        summary=summary,
        details=details,
        reporter_user_id=reporter_user_id,
        channel_id=normalized_channel,
        related_issue_key=related_issue_key,
        attachments=attachments,
    )
    issue_input = JiraIssueCreateInput(
        summary=summary.strip()[:90],
        description=description,
        labels=["discord-bug", "from-discord"],
        issue_type="Bug",
    )
    settings = get_settings()
    try:
        oauth = _tenant_jira_oauth_context(session=session, tenant=tenant, settings=settings)
        create_result = oauth["client"].create_issues_bulk(
            access_token=oauth["access_token"],
            cloud_id=oauth["connection"].cloud_id,
            project_key=project_key,
            issues=[issue_input],
        )
    except (ValueError, JiraOAuthError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to create Jira bug: {exc}",
        ) from exc

    if not create_result.created:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Jira bug create returned no issues: {'; '.join(create_result.errors) or 'unknown error'}",
        )

    created_issue = create_result.created[0]
    uploaded_count, upload_warnings = _upload_discord_attachments_to_jira(
        client=oauth["client"],
        access_token=oauth["access_token"],
        cloud_id=oauth["connection"].cloud_id,
        issue_key=created_issue.key,
        attachments=attachments,
    )
    browse_base_url = str(oauth["connection"].site_url or "").strip().rstrip("/")
    issue_url = build_jira_issue_url(issue_key=created_issue.key, browse_base_url=browse_base_url)
    if issue_url:
        message = f"Bug logged: [{created_issue.key}]({issue_url})"
    else:
        message = f"Bug logged: {created_issue.key}"
    if attachments:
        message = f"{message}. Attached {uploaded_count}/{len(attachments)} file(s) to Jira."
    if create_result.errors:
        message = f"{message} (warnings: {'; '.join(create_result.errors)})"
    if upload_warnings:
        message = f"{message} (attachment warnings: {'; '.join(upload_warnings)})"
    return (
        message,
        {
            "created_issue_keys": [created_issue.key],
            "created_issue_links": [issue_url] if issue_url else [],
            "issue_type": "Bug",
            "project_key": project_key,
            "uploaded_attachment_count": uploaded_count,
            "attachment_warnings": upload_warnings,
        },
    )


def _collect_ask_context(
    *,
    session: Session,
    tenant: Tenant,
    channel_id: str | None,
    question: str,
    scoped_issue_key: str | None = None,
) -> tuple[str | None, str | None, list[dict], dict[str, int]]:
    project_jql = _project_filter_jql(session=session, tenant=tenant, channel_id=channel_id)
    if scoped_issue_key:
        normalized_issue_key = scoped_issue_key.strip().upper()
        jira_issues = _search_jira_issues_for_tenant(
            session=session,
            tenant=tenant,
            jql=f'{project_jql} AND key = "{normalized_issue_key}"',
            max_results=1,
        )
        if not jira_issues:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Issue {normalized_issue_key} was not found for this tenant",
            )
    else:
        normalized_issue_key = None

    lowered = question.strip().lower()
    status_queries = {
        "blocked": "Blocked",
        "in progress": "In Progress",
        "to do": "To Do",
        "testing": "Testing",
        "done": "Done",
        "ready to release": "READY TO RELEASE",
    }
    requested_status = None
    for needle, status_name in status_queries.items():
        if needle in lowered:
            requested_status = status_name
            break

    if normalized_issue_key is None and requested_status:
        jira_issues = _search_jira_issues_for_tenant(
            session=session,
            tenant=tenant,
            jql=f'{project_jql} AND status = "{requested_status}" ORDER BY updated DESC',
            max_results=30,
        )
    elif normalized_issue_key is None:
        jira_issues = _search_jira_issues_for_tenant(
            session=session,
            tenant=tenant,
            jql=f"{project_jql} ORDER BY updated DESC",
            max_results=60,
        )

    issues = [
        {
            "key": issue.key,
            "summary": issue.summary,
            "status": issue.status,
        }
        for issue in jira_issues
    ]
    status_counts: dict[str, int] = {}
    for issue in issues:
        issue_status = issue["status"]
        status_counts[issue_status] = status_counts.get(issue_status, 0) + 1

    return normalized_issue_key, requested_status, issues, status_counts


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
) -> tuple[str, dict]:
    project_keys = _tenant_project_keys(session=session, tenant=tenant)
    if not project_keys:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Tenant has no Jira project keys")

    settings = get_settings()
    runtime = build_codex_runtime(session=session, settings=settings)
    try:
        plan_payload = plan_seed_issues_with_codex(
            runtime=runtime,
            prompt_markdown=prompt_markdown,
            allowed_project_keys=project_keys,
        )
    except CodexRuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Codex issue seeding is unavailable: {exc}",
        ) from exc

    project_key = str(plan_payload.get("project_key") or project_keys[0]).strip().upper()
    if project_key not in project_keys:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Codex selected unsupported Jira project key '{project_key}'",
        )

    raw_issues = plan_payload.get("issues")
    if not isinstance(raw_issues, list):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Codex did not return issue drafts")

    clarification_questions: list[str] = []
    question_set: set[str] = set()
    raw_questions = plan_payload.get("questions")
    if isinstance(raw_questions, list):
        for raw_question in raw_questions:
            question = str(raw_question).strip()
            if question and question not in question_set:
                question_set.add(question)
                clarification_questions.append(question)
    normalized_force_issue_keys = [
        str(value).strip().upper() for value in (force_issue_keys or []) if str(value).strip()
    ]
    issue_inputs: list[JiraIssueCreateInput] = []
    issue_requested_keys: list[str | None] = []
    for item in raw_issues[:12]:
        if not isinstance(item, dict):
            continue
        summary = str(item.get("summary") or "").strip()
        objective = str(item.get("objective") or "").strip()
        scope_in = _normalize_seed_issue_scope(item.get("scope_in"))
        scope_out = _normalize_seed_issue_scope(item.get("scope_out"))
        acceptance_raw = item.get("acceptance_criteria")
        acceptance = (
            [str(entry).strip() for entry in acceptance_raw if str(entry).strip()]
            if isinstance(acceptance_raw, list)
            else []
        )
        requested_issue_key = _normalize_seed_issue_key(item.get("issue_key"))
        if not requested_issue_key and len(normalized_force_issue_keys) > len(issue_requested_keys):
            requested_issue_key = normalized_force_issue_keys[len(issue_requested_keys)]
        tags = _normalize_seed_issue_tags(item.get("tags"))
        labels = _normalize_seed_issue_labels(item.get("labels"))
        for tag in tags:
            if tag not in labels:
                labels.append(tag)
        if not summary:
            continue
        draft_questions = _collect_seed_issue_questions(
            issue_summary=summary,
            objective=objective,
            scope_in=scope_in,
            scope_out=scope_out,
            acceptance=acceptance,
        )
        for question in draft_questions:
            if question not in question_set:
                question_set.add(question)
                clarification_questions.append(question)
        issue_inputs.append(
            JiraIssueCreateInput(
                summary=summary[:90],
                description=_build_seed_issue_description(
                    objective=objective,
                    scope_in=scope_in,
                    scope_out=scope_out,
                    acceptance_criteria=acceptance,
                ),
                labels=labels,
                issue_type=_parse_seed_issue_type(item.get("issue_type")),
            )
        )
        issue_requested_keys.append(requested_issue_key)

    if not issue_inputs:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Codex returned no valid issue drafts")

    try:
        oauth = _tenant_jira_oauth_context(session=session, tenant=tenant, settings=settings)
        existing_issues = oauth["client"].search_issues_by_jql(
            access_token=oauth["access_token"],
            cloud_id=oauth["connection"].cloud_id,
            jql=f'project = "{project_key}" ORDER BY updated DESC',
            max_results=100,
        )
        matched_issue_keys: set[str] = set()
        to_create: list[JiraIssueCreateInput] = []
        updated_issue_keys: list[str] = []

        for idx, issue_input in enumerate(issue_inputs):
            requested_issue_key = issue_requested_keys[idx] if idx < len(issue_requested_keys) else None
            matched = _select_seed_match(
                existing_issues=existing_issues,
                summary=issue_input.summary,
                requested_issue_key=requested_issue_key,
                matched_issue_keys=matched_issue_keys,
            )
            if matched is None:
                to_create.append(issue_input)
                continue
            matched_issue_keys.add(matched.key)
            oauth["client"].update_issue_fields(
                access_token=oauth["access_token"],
                cloud_id=oauth["connection"].cloud_id,
                issue_id_or_key=matched.key,
                summary=issue_input.summary,
                description=issue_input.description,
                labels=issue_input.labels,
            )
            updated_issue_keys.append(matched.key)

        create_result = (
            oauth["client"].create_issues_bulk(
                access_token=oauth["access_token"],
                cloud_id=oauth["connection"].cloud_id,
                project_key=project_key,
                issues=to_create,
            )
            if to_create and allow_create
            else None
        )
    except (ValueError, JiraOAuthError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to seed Jira issues: {exc}",
        ) from exc

    created_keys = [issue.key for issue in create_result.created] if create_result else []
    create_errors = create_result.errors if create_result else []
    if not created_keys and not updated_issue_keys:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Jira seed upsert produced no changes: {'; '.join(create_errors) or 'unknown error'}",
        )
    browse_base_url = str(oauth["connection"].site_url or "").strip().rstrip("/")

    message = (
        "Issue upsert complete. "
        f"Updated {len(updated_issue_keys)}: "
        f"{format_issue_markdown_list(issue_keys=updated_issue_keys, browse_base_url=browse_base_url)}. "
        f"Created {len(created_keys)}: "
        f"{format_issue_markdown_list(issue_keys=created_keys, browse_base_url=browse_base_url)}."
    )
    if create_errors:
        message = f"{message} (partial errors: {'; '.join(create_errors)})"
    if clarification_questions:
        prompt = "\n".join(f"{idx}. {question}" for idx, question in enumerate(clarification_questions, start=1))
        message = (
            f"{message}\n\nI still need more detail to finish issue quality. "
            "Reply in the follow-up thread and I will update these tickets.\n"
            f"{prompt}"
        )
    return (
        message,
        {
            "project_key": project_key,
            "requires_input": bool(clarification_questions),
            "questions": clarification_questions,
            "prompt_markdown": prompt_markdown,
            "updated_issue_keys": updated_issue_keys,
            "updated_issue_links": build_issue_url_list(
                issue_keys=updated_issue_keys,
                browse_base_url=browse_base_url,
            ),
            "created_issue_keys": created_keys,
            "created_issue_links": build_issue_url_list(
                issue_keys=created_keys,
                browse_base_url=browse_base_url,
            ),
            "all_issue_keys": [*updated_issue_keys, *created_keys],
            "errors": create_errors,
        },
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
    normalized_issue_key, requested_status, issues, status_counts, history_context = _collect_ask_context_with_history_context(
        session=session,
        tenant=tenant,
        user_id=user_id,
        channel_id=channel_id,
        question=question,
        scoped_issue_key=scoped_issue_key,
    )

    settings = get_settings()
    runtime = build_codex_runtime(session=session, settings=settings)
    scoped_project_keys = _tenant_project_keys(session=session, tenant=tenant)
    normalized_scope_channel_id = _normalize_scope_channel_id(channel_id)
    if normalized_scope_channel_id:
        scope = _channel_scope_repository.resolve_project_scope(
            session=session,
            tenant=tenant,
            channel_id=normalized_scope_channel_id,
        )
        if scope is not None:
            scoped_project_keys = [scope.jira_project_key]
    try:
        message = answer_board_question_with_codex(
            runtime=runtime,
            question=question,
            project_keys=[str(key).strip().upper() for key in scoped_project_keys if str(key).strip()],
            issues=issues,
            status_counts=status_counts,
            history=history_context,
        )
    except CodexRuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Codex board assistant is unavailable: {exc}",
        ) from exc

    _store_ask_history_entry(
        session=session,
        tenant=tenant,
        user_id=user_id,
        channel_id=channel_id,
        question=question,
        answer=message,
        issue_key=normalized_issue_key,
        status_name=requested_status,
    )

    return (
        message,
        {
            "issue_key": normalized_issue_key,
            "status": requested_status,
            "status_counts": status_counts,
            "issues": issues,
            "question": question,
            "memory_entries_used": len(history_context),
        },
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
    handlers = DiscordIngressHandlers(
        simple=lambda ctx: dispatch_simple_discord_command(
            session=ctx.session,
            tenant=ctx.tenant,
            tenant_id=ctx.tenant_id,
            payload=ctx.payload,
            command_name=ctx.command_name,
            arguments=list(ctx.arguments),
            jira_browse_base_url=resolve_tenant_jira_browse_base_url(session=ctx.session, tenant=ctx.tenant),
            scope=ctx.scope,
        ),
        ask=lambda ctx: dispatch_ask_command(
            session=ctx.session,
            tenant=ctx.tenant,
            payload=ctx.payload,
            command_name=ctx.command_name,
            arguments=list(ctx.arguments),
            normalized_user_id=ctx.normalized_user_id,
            normalized_channel_id=ctx.normalized_channel_id,
            require_ask_confirmation=bool(ctx.flags.get("require_ask_confirmation")),
            issue_key_pattern=ISSUE_KEY_PATTERN,
            collect_ask_context_with_history_context=_collect_ask_context_with_history_context,
            store_pending_ask_action=_store_pending_ask_action,
            store_ask_history_entry=_store_ask_history_entry,
            ask_board_message=_ask_board_message,
            scoped_project_keys=list(ctx.scope.project_keys),
        ),
        bug_gap=lambda ctx: dispatch_bug_gap_command(
            session=ctx.session,
            tenant=ctx.tenant,
            payload=ctx.payload,
            command_name=ctx.command_name,
            arguments=list(ctx.arguments),
            issue_key_pattern=ISSUE_KEY_PATTERN,
            run_gap_analysis=_run_gap_analysis,
            normalize_discord_attachments=_normalize_discord_attachments,
            create_discord_bug_issue=_create_discord_bug_issue,
        ),
        issues=lambda ctx: dispatch_issues_command(
            session=ctx.session,
            tenant=ctx.tenant,
            payload=ctx.payload,
            command_name=ctx.command_name,
            arguments=list(ctx.arguments),
            normalized_user_id=ctx.normalized_user_id,
            defer_seed_issues=bool(ctx.flags.get("defer_seed_issues")),
            seed_issues_with_codex=_seed_issues_with_codex,
            find_seed_followup_context=_find_seed_followup_context,
            store_seed_followup_context=_store_seed_followup_context,
            clear_seed_followup_context=_clear_seed_followup_context,
        ),
        run_control=lambda ctx: dispatch_run_control_command(
            session=ctx.session,
            tenant=ctx.tenant,
            tenant_id=ctx.tenant_id,
            payload=ctx.payload,
            command_name=ctx.command_name,
            arguments=list(ctx.arguments),
            scope=ctx.scope,
            retryable_statuses=RETRYABLE_STATUSES,
            resolve_project_for_issue=_resolve_project_for_issue,
            fetch_issue_preview=_fetch_jira_issue_preview,
            ensure_issue_is_executable=_ensure_issue_is_executable,
        ),
    )
    deps = DiscordIngressDependencies(
        get_tenant=lambda db, current_tenant_id: db.get(Tenant, current_tenant_id),
        resolve_discord_command=lambda current_tenant, raw_command, channel_id, allow_plain: resolve_discord_command(
            tenant=current_tenant,
            raw_command=raw_command,
            channel_id=channel_id,
            allow_plain_ask=allow_plain,
        ),
        assert_channel_scope=lambda db, current_tenant, channel_id: _assert_channel_scope(
            session=db,
            tenant=current_tenant,
            channel_id=channel_id,
        ),
        assert_sensitive_command_permission=lambda db, current_tenant, command_name, user_id, channel_id: _assert_sensitive_command_permission(
            session=db,
            tenant=current_tenant,
            command_name=command_name,
            user_id=user_id,
            channel_id=channel_id,
        ),
        resolve_scope=lambda db, current_tenant, channel_id: _resolve_command_scope(
            session=db,
            tenant=current_tenant,
            channel_id=channel_id,
        ),
        handlers=handlers,
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


register_tenant_command_executor(execute_tenant_command_ingress)
