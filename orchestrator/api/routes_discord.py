from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus
from urllib.request import Request, urlopen
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.api.dependencies import get_session
from orchestrator.api.discord_command_dispatcher import dispatch_simple_discord_command
from orchestrator.api.discord_command_bug_gap import dispatch_bug_gap_command
from orchestrator.api.discord_command_issues import dispatch_issues_command
from orchestrator.api.discord_command_parser import resolve_discord_command
from orchestrator.api.discord_command_run_controls import dispatch_run_control_command
from orchestrator.api.discord_command_ask import dispatch_ask_command
from orchestrator.api.discord_channel_scope_repository import SqlAlchemyDiscordChannelScopeRepository
from orchestrator.api.discord_response_format import (
    build_issue_url_list,
    build_jira_issue_url,
    format_issue_markdown_link,
    format_issue_markdown_list,
)
from orchestrator.api.discord_state import (
    assert_channel_scope as _assert_channel_scope,
    assert_sensitive_command_permission as _assert_sensitive_command_permission,
    clear_seed_followup_context as _clear_seed_followup_context,
    find_seed_followup_context as _find_seed_followup_context,
    normalize_status_name as _normalize_status_name,
    store_seed_followup_context as _store_seed_followup_context,
)
from orchestrator.api.routes_admin import _jira_oauth_client, _refresh_jira_connection_tokens
from orchestrator.api.schemas import DiscordCommandRequest, DiscordCommandResponse
from orchestrator.core.codex_agents import (
    answer_board_question_with_codex,
    plan_seed_issues_with_codex,
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
from orchestrator.storage.models import JiraOAuthConnection, Project, Run, Tenant
from orchestrator.tools.discord_api import DiscordApiClient, DiscordApiError
from orchestrator.tools.jira_oauth import JiraIssueCreateInput, JiraIssueDetail, JiraIssuePreview, JiraOAuthError

router = APIRouter(tags=["discord"])
_channel_scope_repository = SqlAlchemyDiscordChannelScopeRepository()

RETRYABLE_STATUSES = {RUN_STATUS_FAILED, RUN_STATUS_BLOCKED, RUN_STATUS_CANCELLED}
ISSUE_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")
MAX_PENDING_ASK_ACTIONS = 50
MAX_ASK_HISTORY_ENTRIES = 80
MAX_ASK_HISTORY_CONTEXT = 6
GAP_HEADING_STOP_WORDS = {
    "objective",
    "scope",
    "scope in",
    "scope out",
    "how to test",
    "notes",
    "decision gate",
    "good to do",
}


def _fetch_jira_issue_preview(
    *,
    session: Session,
    tenant: Tenant,
    issue_key: str,
) -> JiraIssuePreview:
    settings = get_settings()
    connection_id = str(tenant.jira_config.get("connection_id") or "").strip()
    if not connection_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Jira OAuth connection is not linked for this tenant",
        )
    connection = session.get(JiraOAuthConnection, connection_id)
    if connection is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Configured Jira connection was not found",
        )
    try:
        access_token = _refresh_jira_connection_tokens(
            session,
            connection=connection,
            settings=settings,
        )
        client = _jira_oauth_client(session=session, settings=settings)
        issues = client.search_issues_by_jql(
            access_token=access_token,
            cloud_id=connection.cloud_id,
            jql=f'key = "{issue_key}"',
            max_results=1,
        )
    except (ValueError, JiraOAuthError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to query Jira issue status: {exc}",
        ) from exc

    if not issues:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Issue {issue_key} was not found in Jira",
        )
    return issues[0]


def _fetch_jira_issue_detail(
    *,
    session: Session,
    tenant: Tenant,
    issue_key: str,
) -> JiraIssueDetail:
    settings = get_settings()
    connection_id = str(tenant.jira_config.get("connection_id") or "").strip()
    if not connection_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Jira OAuth connection is not linked for this tenant",
        )
    connection = session.get(JiraOAuthConnection, connection_id)
    if connection is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Configured Jira connection was not found",
        )
    try:
        access_token = _refresh_jira_connection_tokens(
            session,
            connection=connection,
            settings=settings,
        )
        client = _jira_oauth_client(session=session, settings=settings)
        return client.get_issue_detail(
            access_token=access_token,
            cloud_id=connection.cloud_id,
            issue_id_or_key=issue_key,
        )
    except (ValueError, JiraOAuthError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to query Jira issue details: {exc}",
        ) from exc


def _tenant_repo_url(tenant: Tenant) -> str | None:
    repo_url = str((tenant.repos_config or {}).get("github_repository") or "").strip()
    return repo_url or None


def _tenant_jira_browse_base_url(*, session: Session, tenant: Tenant) -> str | None:
    connection_id = str(tenant.jira_config.get("connection_id") or "").strip()
    if not connection_id:
        return None
    connection = session.get(JiraOAuthConnection, connection_id)
    if connection is None:
        return None
    normalized_site_url = str(connection.site_url or "").strip().rstrip("/")
    return normalized_site_url or None


def _extract_acceptance_criteria_from_description(description: str) -> list[str]:
    lines = [line.strip() for line in description.splitlines() if line.strip()]
    if not lines:
        return []

    acceptance_lines: list[str] = []
    in_acceptance_section = False
    for line in lines:
        normalized = line.lower().rstrip(":")
        if not in_acceptance_section and normalized == "acceptance criteria":
            in_acceptance_section = True
            continue
        if in_acceptance_section:
            if normalized in GAP_HEADING_STOP_WORDS:
                break
            cleaned = line.lstrip("-*• ").strip()
            if cleaned:
                acceptance_lines.append(cleaned)
            continue
        if normalized.startswith("acceptance criteria"):
            remainder = line.split(":", 1)[1].strip() if ":" in line else ""
            if remainder:
                acceptance_lines.append(remainder)
                in_acceptance_section = True

    if acceptance_lines:
        return acceptance_lines[:8]

    fallback: list[str] = []
    for line in lines:
        normalized = line.lower()
        if any(token in normalized for token in ("must", "should", "returns", "include", "supports")):
            fallback.append(line.lstrip("-*• ").strip())
    return fallback[:5]


def _gap_confidence(*, has_acceptance: bool, has_successful_run: bool, has_pr: bool) -> str:
    score = int(has_acceptance) + int(has_successful_run) + int(has_pr)
    if score >= 3:
        return "high"
    if score == 2:
        return "medium"
    return "low"


def _run_gap_analysis(
    *,
    session: Session,
    tenant: Tenant,
    issue_key: str,
) -> tuple[str, dict]:
    normalized_issue_key = issue_key.strip().upper()
    if not ISSUE_KEY_PATTERN.match(normalized_issue_key):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Usage: !gap <ISSUE_KEY>")

    issue = _fetch_jira_issue_detail(session=session, tenant=tenant, issue_key=normalized_issue_key)
    acceptance = _extract_acceptance_criteria_from_description(issue.description)
    latest_run = session.execute(
        select(Run)
        .where(Run.tenant_id == tenant.tenant_id, Run.issue_key == normalized_issue_key)
        .order_by(Run.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()

    jira_base_url = _tenant_jira_browse_base_url(session=session, tenant=tenant)
    jira_url = f"{jira_base_url}/browse/{normalized_issue_key}" if jira_base_url else None
    repo_url = _tenant_repo_url(tenant)
    repo_issue_search_url = f"{repo_url}/search?q={quote_plus(normalized_issue_key)}" if repo_url else None
    pr_url = str(latest_run.pr_url or "").strip() if latest_run else ""
    has_successful_run = latest_run is not None and latest_run.status == RUN_STATUS_SUCCEEDED
    has_pr = bool(pr_url)
    confidence = _gap_confidence(
        has_acceptance=bool(acceptance),
        has_successful_run=has_successful_run,
        has_pr=has_pr,
    )

    gaps: list[str] = []
    if not acceptance:
        gaps.append("Acceptance criteria are missing or unclear in Jira description.")
    if latest_run is None:
        gaps.append("No run has been executed yet for this issue.")
    elif latest_run.status != RUN_STATUS_SUCCEEDED:
        gaps.append(f"Latest run `{latest_run.run_id}` is `{latest_run.status}` (not `succeeded`).")
    if not pr_url:
        gaps.append("No PR is linked to the latest run.")

    next_actions: list[str] = []
    if not acceptance:
        next_actions.append("Update Jira with explicit acceptance criteria bullets.")
    if latest_run is None or latest_run.status != RUN_STATUS_SUCCEEDED:
        next_actions.append(f"Run `!run {normalized_issue_key}` and resolve failures.")
    if not pr_url:
        next_actions.append("Create or link a PR that implements the issue scope.")
    if not next_actions:
        next_actions.append("Re-validate acceptance criteria against latest merged code before release.")

    issue_label = format_issue_markdown_link(
        issue_key=normalized_issue_key,
        browse_base_url=jira_base_url,
    )
    lines = [
        f"Gap analysis for {issue_label}: {issue.summary}",
        f"Confidence: **{confidence}**",
        "",
        "Evidence:",
    ]
    if latest_run is None:
        lines.append("- Latest run: none")
    else:
        lines.append(
            f"- Latest run: `{latest_run.run_id}` ({latest_run.status})"
            + (f" | PR: [open]({pr_url})" if pr_url else "")
        )
    if repo_issue_search_url:
        lines.append(f"- Code reference: [repo search for {normalized_issue_key}]({repo_issue_search_url})")

    lines.extend(["", "Acceptance Criteria:"])
    if acceptance:
        for criterion in acceptance[:6]:
            criterion_search = f"{repo_url}/search?q={quote_plus(criterion)}" if repo_url else None
            if criterion_search:
                lines.append(f"- {criterion} ([code search]({criterion_search}))")
            else:
                lines.append(f"- {criterion}")
    else:
        lines.append("- None detected in Jira description.")

    lines.extend(["", "Gaps:"])
    if gaps:
        lines.extend([f"- {gap}" for gap in gaps])
    else:
        lines.append("- No obvious gaps detected from Jira + latest run metadata.")

    lines.extend(["", "Next actions:"])
    lines.extend([f"- {action}" for action in next_actions[:4]])

    return (
        "\n".join(lines),
        {
            "issue_key": normalized_issue_key,
            "jira_url": jira_url,
            "pr_url": pr_url or None,
            "repo_search_url": repo_issue_search_url,
            "latest_run_id": latest_run.run_id if latest_run else None,
            "latest_run_status": latest_run.status if latest_run else None,
            "acceptance_criteria": acceptance,
            "gaps": gaps,
            "confidence": confidence,
        },
    )


def _search_jira_issues_for_tenant(
    *,
    session: Session,
    tenant: Tenant,
    jql: str,
    max_results: int = 20,
) -> list[JiraIssuePreview]:
    settings = get_settings()
    connection_id = str(tenant.jira_config.get("connection_id") or "").strip()
    if not connection_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Jira OAuth connection is not linked for this tenant",
        )
    connection = session.get(JiraOAuthConnection, connection_id)
    if connection is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Configured Jira connection was not found",
        )
    try:
        access_token = _refresh_jira_connection_tokens(
            session,
            connection=connection,
            settings=settings,
        )
        client = _jira_oauth_client(session=session, settings=settings)
        return client.search_issues_by_jql(
            access_token=access_token,
            cloud_id=connection.cloud_id,
            jql=jql,
            max_results=max_results,
        )
    except (ValueError, JiraOAuthError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to query Jira board: {exc}",
        ) from exc


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


def _project_filter_jql(*, session: Session, tenant: Tenant, channel_id: str | None = None) -> str:
    if channel_id:
        scope = _channel_scope_repository.resolve_project_scope(
            session=session,
            tenant=tenant,
            channel_id=channel_id,
        )
        if scope is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Discord channel is not mapped to an active project",
            )
        return f'project = "{scope.jira_project_key}"'
    keys = [project.jira_project_key for project in _tenant_active_projects(session=session, tenant_id=tenant.tenant_id)]
    if not keys:
        keys = [str(key).strip().upper() for key in tenant.jira_config.get("project_keys", []) if str(key).strip()]
    if not keys:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Tenant has no Jira project keys")
    if len(keys) == 1:
        return f'project = "{keys[0]}"'
    joined = ", ".join(f'"{key}"' for key in keys)
    return f"project in ({joined})"


def _tenant_project_keys(*, session: Session, tenant: Tenant) -> list[str]:
    keys = [project.jira_project_key for project in _tenant_active_projects(session=session, tenant_id=tenant.tenant_id)]
    if keys:
        return keys
    return [str(key).strip().upper() for key in tenant.jira_config.get("project_keys", []) if str(key).strip()]


def _tenant_active_projects(*, session: Session, tenant_id: str) -> list[Project]:
    return session.execute(
        select(Project)
        .where(Project.tenant_id == tenant_id, Project.is_archived.is_(False))
        .order_by(Project.created_at.asc())
    ).scalars().all()


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
    if not isinstance(raw_labels, list):
        return ["discord-seeded"]
    normalized: list[str] = ["discord-seeded"]
    for label in raw_labels:
        text = str(label).strip().lower()
        if text and text not in normalized:
            normalized.append(text)
    return normalized


def _normalize_seed_issue_tags(raw_tags: object) -> list[str]:
    if not isinstance(raw_tags, list):
        return []
    normalized: list[str] = []
    for tag in raw_tags:
        text = str(tag).strip().lower()
        if text and text not in normalized:
            normalized.append(text)
    return normalized


def _parse_seed_issue_type(raw_issue_type: object) -> str:
    normalized = str(raw_issue_type or "").strip().lower()
    if normalized == "bug":
        return "Bug"
    if normalized == "story":
        return "Story"
    return "Task"


def _normalize_seed_issue_scope(raw_scope: object) -> list[str]:
    if not isinstance(raw_scope, list):
        return []
    return [str(item).strip() for item in raw_scope if str(item).strip()]


def _normalize_seed_issue_key(raw_issue_key: object) -> str | None:
    normalized = str(raw_issue_key or "").strip().upper()
    if ISSUE_KEY_PATTERN.match(normalized):
        return normalized
    return None


def _seed_text_is_missing(value: str) -> bool:
    normalized = value.strip().lower()
    if not normalized:
        return True
    if normalized in {"tbd", "unknown", "n/a", "na", "none", "todo", "decide later"}:
        return True
    if "to be determined" in normalized:
        return True
    if "???" in normalized:
        return True
    return False


def _collect_seed_issue_questions(*, issue_summary: str, objective: str, scope_in: list[str], scope_out: list[str], acceptance: list[str]) -> list[str]:
    questions: list[str] = []
    title = issue_summary.strip() or "this issue"
    if _seed_text_is_missing(objective):
        questions.append(f"For '{title}', what is the objective in one sentence?")
    if not scope_in:
        questions.append(f"For '{title}', what is explicitly in scope?")
    if not scope_out:
        questions.append(f"For '{title}', what is explicitly out of scope?")
    if not acceptance:
        questions.append(f"For '{title}', list acceptance criteria (at least 1 testable outcome).")
    return questions


def _normalized_summary_key(summary: str) -> str:
    return " ".join(part for part in re.split(r"[^a-z0-9]+", summary.lower()) if part)


def _summary_similarity(left: str, right: str) -> float:
    left_tokens = {token for token in re.split(r"[^a-z0-9]+", left.lower()) if token}
    right_tokens = {token for token in re.split(r"[^a-z0-9]+", right.lower()) if token}
    if not left_tokens or not right_tokens:
        return 0.0
    intersection = left_tokens.intersection(right_tokens)
    union = left_tokens.union(right_tokens)
    return len(intersection) / max(1, len(union))


def _select_seed_match(
    *,
    existing_issues: list[JiraIssuePreview],
    summary: str,
    requested_issue_key: str | None,
    matched_issue_keys: set[str],
) -> JiraIssuePreview | None:
    if requested_issue_key:
        for issue in existing_issues:
            if issue.key == requested_issue_key and issue.key not in matched_issue_keys:
                return issue

    normalized_target = _normalized_summary_key(summary)
    if not normalized_target:
        return None

    for issue in existing_issues:
        if issue.key in matched_issue_keys:
            continue
        if _normalized_summary_key(issue.summary) == normalized_target:
            return issue

    best_issue: JiraIssuePreview | None = None
    best_score = 0.0
    for issue in existing_issues:
        if issue.key in matched_issue_keys:
            continue
        score = _summary_similarity(summary, issue.summary)
        if score > best_score:
            best_score = score
            best_issue = issue
    if best_issue is not None and best_score >= 0.66:
        return best_issue
    return None


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
    if not isinstance(raw_attachments, list):
        return []
    normalized: list[dict[str, str]] = []
    for item in raw_attachments:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        filename = str(item.get("filename") or "").strip() or "attachment"
        content_type = str(item.get("content_type") or "").strip()
        normalized.append(
            {
                "url": url,
                "filename": filename,
                "content_type": content_type,
            }
        )
    return normalized[:5]


def _build_discord_bug_description(
    *,
    summary: str,
    details: str,
    reporter_user_id: str,
    channel_id: str | None,
    related_issue_key: str | None,
    attachments: list[dict[str, str]],
) -> str:
    lines = [
        "Reported via Discord",
        f"- Reporter: {reporter_user_id}",
        f"- Channel: {channel_id or 'unknown'}",
        f"- Reported at: {datetime.now(timezone.utc).isoformat()}",
    ]
    if related_issue_key:
        lines.append(f"- Related issue: {related_issue_key}")
    lines.extend(["", "Summary", summary.strip(), "", "Context"])
    lines.append(details.strip() or "No additional context provided.")
    if attachments:
        lines.extend(["", "Attachments"])
        for attachment in attachments:
            filename = attachment.get("filename") or "attachment"
            url = attachment.get("url") or ""
            content_type = attachment.get("content_type") or ""
            if content_type:
                lines.append(f"- [{filename}]({url}) ({content_type})")
            else:
                lines.append(f"- [{filename}]({url})")
    return "\n".join(lines)


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
    connection_id = str(tenant.jira_config.get("connection_id") or "").strip()
    if not connection_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Jira OAuth connection is not linked for this tenant",
        )
    connection = session.get(JiraOAuthConnection, connection_id)
    if connection is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Configured Jira connection was not found",
        )

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
        access_token = _refresh_jira_connection_tokens(
            session,
            connection=connection,
            settings=settings,
        )
        client = _jira_oauth_client(session=session, settings=settings)
        create_result = client.create_issues_bulk(
            access_token=access_token,
            cloud_id=connection.cloud_id,
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
        client=client,
        access_token=access_token,
        cloud_id=connection.cloud_id,
        issue_key=created_issue.key,
        attachments=attachments,
    )
    browse_base_url = str(connection.site_url or "").strip().rstrip("/")
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
    remove_issue_key_from_tenant_ask_history(
        session=session,
        tenant=tenant,
        issue_key=issue_key,
        user_id=user_id,
        channel_id=channel_id,
    )


def remove_issue_key_from_tenant_ask_history(
    *,
    session: Session,
    tenant: Tenant,
    issue_key: str,
    user_id: str | None = None,
    channel_id: str | None = None,
) -> int:
    target_issue_key = issue_key.strip().upper()
    if not target_issue_key:
        return 0

    entries = _tenant_ask_history(tenant)
    kept_entries: list[dict] = []
    removed_count = 0
    for entry in entries:
        entry_issue_key = str(entry.get("issue_key") or "").strip().upper()
        matches_scope = True
        if user_id is not None:
            matches_scope = matches_scope and entry.get("user_id") == user_id
        if channel_id is not None:
            matches_scope = matches_scope and entry.get("channel_id") == channel_id
        if matches_scope and entry_issue_key == target_issue_key:
            removed_count += 1
            continue
        kept_entries.append(entry)

    if removed_count == 0:
        return 0

    discord_config = dict(tenant.discord_config or {})
    discord_config["ask_history"] = kept_entries[-MAX_ASK_HISTORY_ENTRIES:]
    tenant.discord_config = discord_config
    tenant.updated_at = datetime.now(timezone.utc)
    session.commit()
    return removed_count


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
    connection_id = str((tenant.jira_config or {}).get("connection_id") or "").strip()
    if not connection_id:
        return 0

    entries = _tenant_ask_history(tenant)
    scoped_issue_keys = {
        str(entry.get("issue_key") or "").strip().upper()
        for entry in entries
        if entry.get("user_id") == user_id and entry.get("channel_id") == channel_id
    }
    scoped_issue_keys.discard("")
    if not scoped_issue_keys:
        return 0

    existing_issue_keys = _existing_issue_keys_for_tenant(
        session=session,
        tenant=tenant,
        channel_id=channel_id,
        issue_keys=scoped_issue_keys,
    )
    missing_issue_keys = scoped_issue_keys - existing_issue_keys
    if not missing_issue_keys:
        return 0

    removed_count = 0
    for issue_key in sorted(missing_issue_keys):
        removed_count += remove_issue_key_from_tenant_ask_history(
            session=session,
            tenant=tenant,
            issue_key=issue_key,
            user_id=user_id,
            channel_id=channel_id,
        )
    return removed_count


def _collect_ask_context_with_history_context(
    *,
    session: Session,
    tenant: Tenant,
    user_id: str,
    channel_id: str,
    question: str,
    scoped_issue_key: str | None,
) -> tuple[str | None, str | None, list[dict], dict[str, int], list[dict]]:
    history_context = _recent_ask_history(
        tenant=tenant,
        user_id=user_id,
        channel_id=channel_id,
        limit=MAX_ASK_HISTORY_CONTEXT,
    )
    if _prune_missing_issue_keys_from_ask_history(
        session=session,
        tenant=tenant,
        user_id=user_id,
        channel_id=channel_id,
    ):
        history_context = _recent_ask_history(
            tenant=tenant,
            user_id=user_id,
            channel_id=channel_id,
            limit=MAX_ASK_HISTORY_CONTEXT,
        )

    resolved_scoped_issue_key = scoped_issue_key
    if resolved_scoped_issue_key is None:
        for entry in reversed(history_context):
            issue_key = str(entry.get("issue_key") or "").strip().upper()
            if issue_key:
                resolved_scoped_issue_key = issue_key
                break

    normalized_issue_key, requested_status, issues, status_counts = _collect_ask_context(
        session=session,
        tenant=tenant,
        channel_id=channel_id,
        question=question,
        scoped_issue_key=resolved_scoped_issue_key,
    )

    return normalized_issue_key, requested_status, issues, status_counts, history_context


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
    discord_config = dict(tenant.discord_config or {})
    raw_pending = discord_config.get("pending_ask_actions")
    pending = [entry for entry in raw_pending if isinstance(entry, dict)] if isinstance(raw_pending, list) else []
    request_id = uuid4().hex
    created_at = datetime.now(timezone.utc).isoformat()
    pending.append(
        {
            "request_id": request_id,
            "user_id": user_id.strip(),
            "channel_id": channel_id.strip() if isinstance(channel_id, str) and channel_id.strip() else None,
            "question": question.strip(),
            "summary": summary.strip(),
            "proposed_command": proposed_command.strip(),
            "created_at": created_at,
        }
    )
    discord_config["pending_ask_actions"] = pending[-MAX_PENDING_ASK_ACTIONS:]
    tenant.discord_config = discord_config
    tenant.updated_at = datetime.now(timezone.utc)
    session.commit()
    return {
        "request_id": request_id,
        "created_at": created_at,
    }


def consume_pending_ask_action(
    *,
    session: Session,
    tenant: Tenant,
    request_id: str,
) -> dict | None:
    normalized_request_id = request_id.strip()
    if not normalized_request_id:
        return None
    discord_config = dict(tenant.discord_config or {})
    raw_pending = discord_config.get("pending_ask_actions")
    pending = [entry for entry in raw_pending if isinstance(entry, dict)] if isinstance(raw_pending, list) else []
    matched: dict | None = None
    kept: list[dict] = []
    for entry in pending:
        entry_id = str(entry.get("request_id") or "").strip()
        if matched is None and entry_id == normalized_request_id:
            matched = entry
            continue
        kept.append(entry)
    if matched is None:
        return None
    discord_config["pending_ask_actions"] = kept
    tenant.discord_config = discord_config
    tenant.updated_at = datetime.now(timezone.utc)
    session.commit()
    return matched


def _tenant_ask_history(tenant: Tenant) -> list[dict]:
    discord_config = tenant.discord_config or {}
    raw_history = discord_config.get("ask_history")
    if not isinstance(raw_history, list):
        return []
    normalized: list[dict] = []
    for entry in raw_history:
        if not isinstance(entry, dict):
            continue
        user_id = str(entry.get("user_id") or "").strip()
        channel_id = str(entry.get("channel_id") or "").strip()
        question = str(entry.get("question") or "").strip()
        answer = str(entry.get("answer") or "").strip()
        if not user_id or not channel_id or not question or not answer:
            continue
        normalized.append(
            {
                "user_id": user_id,
                "channel_id": channel_id,
                "question": question,
                "answer": answer,
                "issue_key": str(entry.get("issue_key") or "").strip().upper() or None,
                "status": str(entry.get("status") or "").strip() or None,
                "created_at": str(entry.get("created_at") or "").strip()
                or datetime.now(timezone.utc).isoformat(),
            }
        )
    return normalized


def _recent_ask_history(
    *,
    tenant: Tenant,
    user_id: str,
    channel_id: str,
    limit: int = MAX_ASK_HISTORY_CONTEXT,
) -> list[dict]:
    entries = _tenant_ask_history(tenant)
    scoped = [
        entry
        for entry in entries
        if entry.get("user_id") == user_id and entry.get("channel_id") == channel_id
    ]
    if not scoped:
        return []
    return scoped[-max(1, limit) :]


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
    discord_config = dict(tenant.discord_config or {})
    entries = _tenant_ask_history(tenant)
    entries.append(
        {
            "user_id": user_id,
            "channel_id": channel_id,
            "question": question.strip(),
            "answer": answer.strip(),
            "issue_key": issue_key.strip().upper() if isinstance(issue_key, str) and issue_key.strip() else None,
            "status": status_name.strip() if isinstance(status_name, str) and status_name.strip() else None,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    discord_config["ask_history"] = entries[-MAX_ASK_HISTORY_ENTRIES:]
    tenant.discord_config = discord_config
    tenant.updated_at = datetime.now(timezone.utc)
    session.commit()


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

    connection_id = str(tenant.jira_config.get("connection_id") or "").strip()
    if not connection_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Jira OAuth connection is not linked for this tenant",
        )
    connection = session.get(JiraOAuthConnection, connection_id)
    if connection is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Configured Jira connection was not found",
        )
    try:
        access_token = _refresh_jira_connection_tokens(
            session,
            connection=connection,
            settings=settings,
        )
        client = _jira_oauth_client(session=session, settings=settings)
        existing_issues = client.search_issues_by_jql(
            access_token=access_token,
            cloud_id=connection.cloud_id,
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
            client.update_issue_fields(
                access_token=access_token,
                cloud_id=connection.cloud_id,
                issue_id_or_key=matched.key,
                summary=issue_input.summary,
                description=issue_input.description,
                labels=issue_input.labels,
            )
            updated_issue_keys.append(matched.key)

        create_result = (
            client.create_issues_bulk(
                access_token=access_token,
                cloud_id=connection.cloud_id,
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
    browse_base_url = str(connection.site_url or "").strip().rstrip("/")

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
    try:
        message = answer_board_question_with_codex(
            runtime=runtime,
            question=question,
            project_keys=[str(key).strip().upper() for key in tenant.jira_config.get("project_keys", []) if str(key).strip()],
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


@router.post("/discord/command/{tenant_id}", response_model=DiscordCommandResponse)
def execute_discord_command(
    tenant_id: str,
    payload: DiscordCommandRequest,
    session: Session = Depends(get_session),
    *,
    defer_seed_issues: bool = False,
    require_ask_confirmation: bool = False,
    allow_plain_ask: bool = False,
) -> DiscordCommandResponse:
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown tenant")
    if not tenant.is_enabled:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Tenant is disabled")

    _assert_channel_scope(session=session, tenant=tenant, channel_id=payload.channel_id)
    raw_command = payload.command.strip()
    _, command_name, arguments = resolve_discord_command(
        tenant=tenant,
        raw_command=raw_command,
        channel_id=payload.channel_id,
        allow_plain_ask=allow_plain_ask,
    )
    _assert_sensitive_command_permission(
        session=session,
        tenant=tenant,
        command_name=command_name,
        user_id=payload.user_id,
        channel_id=payload.channel_id,
    )
    normalized_user_id = payload.user_id.strip()
    normalized_channel_id = payload.channel_id.strip() if payload.channel_id else "__dm__"

    simple_response = dispatch_simple_discord_command(
        session=session,
        tenant=tenant,
        tenant_id=tenant_id,
        payload=payload,
        command_name=command_name,
        arguments=arguments,
        jira_browse_base_url=_tenant_jira_browse_base_url(session=session, tenant=tenant),
    )
    if simple_response is not None:
        return simple_response

    ask_response = dispatch_ask_command(
        session=session,
        tenant=tenant,
        payload=payload,
        command_name=command_name,
        arguments=arguments,
        normalized_user_id=normalized_user_id,
        normalized_channel_id=normalized_channel_id,
        require_ask_confirmation=require_ask_confirmation,
        issue_key_pattern=ISSUE_KEY_PATTERN,
        collect_ask_context_with_history_context=_collect_ask_context_with_history_context,
        store_pending_ask_action=_store_pending_ask_action,
        store_ask_history_entry=_store_ask_history_entry,
        ask_board_message=_ask_board_message,
    )
    if ask_response is not None:
        return ask_response

    bug_gap_response = dispatch_bug_gap_command(
        session=session,
        tenant=tenant,
        payload=payload,
        command_name=command_name,
        arguments=arguments,
        issue_key_pattern=ISSUE_KEY_PATTERN,
        run_gap_analysis=_run_gap_analysis,
        normalize_discord_attachments=_normalize_discord_attachments,
        create_discord_bug_issue=_create_discord_bug_issue,
    )
    if bug_gap_response is not None:
        return bug_gap_response

    issues_response = dispatch_issues_command(
        session=session,
        tenant=tenant,
        payload=payload,
        command_name=command_name,
        arguments=arguments,
        normalized_user_id=normalized_user_id,
        defer_seed_issues=defer_seed_issues,
        seed_issues_with_codex=_seed_issues_with_codex,
        find_seed_followup_context=_find_seed_followup_context,
        store_seed_followup_context=_store_seed_followup_context,
        clear_seed_followup_context=_clear_seed_followup_context,
    )
    if issues_response is not None:
        return issues_response

    run_control_response = dispatch_run_control_command(
        session=session,
        tenant=tenant,
        tenant_id=tenant_id,
        payload=payload,
        command_name=command_name,
        arguments=arguments,
        retryable_statuses=RETRYABLE_STATUSES,
        resolve_project_for_issue=_resolve_project_for_issue,
        fetch_issue_preview=_fetch_jira_issue_preview,
        ensure_issue_is_executable=_ensure_issue_is_executable,
    )
    if run_control_response is not None:
        return run_control_response

    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported command")
