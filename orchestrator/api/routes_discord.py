from __future__ import annotations

import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from orchestrator.api.dependencies import get_session
from orchestrator.api.routes_admin import _jira_oauth_client, _refresh_jira_connection_tokens
from orchestrator.api.schemas import DiscordCommandRequest, DiscordCommandResponse
from orchestrator.core.codex_agents import answer_board_question_with_codex, plan_seed_issues_with_codex
from orchestrator.core.codex_runtime import CodexRuntimeError, build_codex_runtime
from orchestrator.core.config import get_settings
from orchestrator.core.runs import (
    RUN_STATUS_BLOCKED,
    RUN_STATUS_CANCELLED,
    RUN_STATUS_FAILED,
    RUN_STATUS_QUEUED,
    RUN_STATUS_RUNNING,
    cancel_run,
    enqueue_run,
)
from orchestrator.storage.models import JiraOAuthConnection, Run, Tenant, WebhookDelivery
from orchestrator.tools.jira_oauth import JiraIssueCreateInput, JiraIssuePreview, JiraOAuthError

router = APIRouter(tags=["discord"])

SENSITIVE_COMMANDS = {"run", "cancel", "retry", "promote", "issues"}
PUBLIC_COMMANDS = {"help", "status", "runs", "policy", "link", "ask", "allowlist"}
SUPPORTED_COMMANDS = SENSITIVE_COMMANDS | PUBLIC_COMMANDS
RETRYABLE_STATUSES = {RUN_STATUS_FAILED, RUN_STATUS_BLOCKED, RUN_STATUS_CANCELLED}
ISSUE_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")


def _normalize_status_name(value: str) -> str:
    return value.strip().lower()


def _parse_command_text(command_text: str) -> tuple[str, list[str]]:
    normalized = command_text.strip()
    if not normalized.startswith("!"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Commands must start with '!'")

    parts = [part for part in normalized[1:].split(" ") if part]
    if not parts:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing command name")

    command_name = parts[0].strip().lower()
    arguments = parts[1:]
    if command_name not in SUPPORTED_COMMANDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported command '{command_name}'",
        )
    return command_name, arguments


def _tenant_allowlisted_user_ids(tenant: Tenant) -> set[str]:
    discord_config = tenant.discord_config or {}
    raw_allowlist = discord_config.get("allowed_user_ids")
    if not isinstance(raw_allowlist, list):
        return set()
    normalized = {str(user_id).strip() for user_id in raw_allowlist if str(user_id).strip()}
    return normalized


def _tenant_allowlist_requests(tenant: Tenant) -> list[dict]:
    discord_config = tenant.discord_config or {}
    raw_requests = discord_config.get("allowlist_requests")
    if not isinstance(raw_requests, list):
        return []
    normalized: list[dict] = []
    for item in raw_requests:
        if not isinstance(item, dict):
            continue
        user_id = str(item.get("user_id") or "").strip()
        if not user_id:
            continue
        normalized.append(
            {
                "user_id": user_id,
                "requested_at": str(item.get("requested_at") or "").strip() or datetime.now(timezone.utc).isoformat(),
                "channel_id": str(item.get("channel_id") or "").strip() or None,
                "reason": str(item.get("reason") or "").strip() or None,
            }
        )
    return normalized


def _create_allowlist_request(
    *,
    session: Session,
    tenant: Tenant,
    user_id: str,
    channel_id: str | None,
    reason: str | None,
) -> tuple[bool, str]:
    allowlisted_ids = _tenant_allowlisted_user_ids(tenant)
    if user_id in allowlisted_ids:
        return False, "You are already allowlisted for sensitive commands."

    requests = _tenant_allowlist_requests(tenant)
    existing = next((entry for entry in requests if entry.get("user_id") == user_id), None)
    now_iso = datetime.now(timezone.utc).isoformat()
    if existing:
        existing["requested_at"] = now_iso
        existing["channel_id"] = channel_id
        existing["reason"] = reason
        message = "Allowlist request refreshed. An admin can approve it in the tenant page."
    else:
        requests.append(
            {
                "user_id": user_id,
                "requested_at": now_iso,
                "channel_id": channel_id,
                "reason": reason,
            }
        )
        message = "Allowlist request submitted. An admin can approve it in the tenant page."

    discord_config = dict(tenant.discord_config or {})
    discord_config["allowlist_requests"] = requests
    tenant.discord_config = discord_config
    tenant.updated_at = datetime.now(timezone.utc)
    session.commit()
    return True, message


def _assert_sensitive_command_permission(*, tenant: Tenant, command_name: str, user_id: str) -> None:
    if command_name not in SENSITIVE_COMMANDS:
        return
    allowlist = _tenant_allowlisted_user_ids(tenant)
    if user_id not in allowlist:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"'{command_name}' requires an allowlisted Discord user",
        )


def _assert_channel_scope(*, tenant: Tenant, channel_id: str | None) -> None:
    if not channel_id:
        return
    discord_config = tenant.discord_config or {}
    configured_channel_id = str(discord_config.get("channel_id") or "").strip()
    if configured_channel_id and configured_channel_id != channel_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Command channel does not match tenant Discord channel",
        )


def _format_elapsed_seconds(*, started_at: datetime | None, created_at: datetime | None) -> int:
    anchor = started_at or created_at
    if anchor is None:
        return 0
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - anchor).total_seconds()))


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


def _command_help_message() -> str:
    return (
        "Commands: !help, !status, !runs [N], !run <ISSUE_KEY>, !cancel <RUN_ID>, "
        "!retry <ISSUE_KEY|RUN_ID>, !policy, !link <ISSUE_KEY>, !ask <question>, "
        "!issues seed <markdown spec>, !allowlist request [reason]"
    )


def _command_policy_message() -> str:
    return (
        "Policy: no secrets in output, no sleep-based synchronization, tests required for behavior "
        "changes, and Decision Gate required when requirements are ambiguous."
    )


def _project_filter_jql(tenant: Tenant) -> str:
    keys = [str(key).strip().upper() for key in tenant.jira_config.get("project_keys", []) if str(key).strip()]
    if not keys:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Tenant has no Jira project keys")
    if len(keys) == 1:
        return f'project = "{keys[0]}"'
    joined = ", ".join(f'"{key}"' for key in keys)
    return f"project in ({joined})"


def _tenant_project_keys(tenant: Tenant) -> list[str]:
    return [str(key).strip().upper() for key in tenant.jira_config.get("project_keys", []) if str(key).strip()]


def _normalize_seed_issue_labels(raw_labels: object) -> list[str]:
    if not isinstance(raw_labels, list):
        return ["discord-seeded"]
    normalized: list[str] = ["discord-seeded"]
    for label in raw_labels:
        text = str(label).strip().lower()
        if text and text not in normalized:
            normalized.append(text)
    return normalized


def _build_seed_issue_description(*, objective: str, acceptance_criteria: list[str]) -> str:
    lines = ["Objective", objective.strip() or "No objective provided", "", "Acceptance Criteria"]
    if acceptance_criteria:
        lines.extend(f"- {criterion}" for criterion in acceptance_criteria)
    else:
        lines.append("- Criteria were not provided")
    return "\n".join(lines)


def _seed_issues_with_codex(*, session: Session, tenant: Tenant, prompt_markdown: str) -> tuple[str, dict]:
    project_keys = _tenant_project_keys(tenant)
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

    issue_inputs: list[JiraIssueCreateInput] = []
    for item in raw_issues[:12]:
        if not isinstance(item, dict):
            continue
        summary = str(item.get("summary") or "").strip()
        objective = str(item.get("objective") or "").strip()
        acceptance_raw = item.get("acceptance_criteria")
        acceptance = (
            [str(entry).strip() for entry in acceptance_raw if str(entry).strip()]
            if isinstance(acceptance_raw, list)
            else []
        )
        if not summary:
            continue
        issue_inputs.append(
            JiraIssueCreateInput(
                summary=summary[:90],
                description=_build_seed_issue_description(
                    objective=objective,
                    acceptance_criteria=acceptance,
                ),
                labels=_normalize_seed_issue_labels(item.get("labels")),
            )
        )

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
        create_result = client.create_issues_bulk(
            access_token=access_token,
            cloud_id=connection.cloud_id,
            project_key=project_key,
            issues=issue_inputs,
        )
    except (ValueError, JiraOAuthError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to create Jira issues: {exc}",
        ) from exc

    created_keys = [issue.key for issue in create_result.created]
    if not created_keys:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Jira bulk create returned no issues: {'; '.join(create_result.errors) or 'unknown error'}",
        )
    message = f"Seeded {len(created_keys)} issue(s): {', '.join(created_keys)}"
    if create_result.errors:
        message = f"{message} (partial errors: {'; '.join(create_result.errors)})"
    return (
        message,
        {
            "project_key": project_key,
            "created_issue_keys": created_keys,
            "errors": create_result.errors,
        },
    )


def _ask_board_message(
    *,
    session: Session,
    tenant: Tenant,
    question: str,
    scoped_issue_key: str | None = None,
) -> tuple[str, dict]:
    project_jql = _project_filter_jql(tenant)
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

    settings = get_settings()
    runtime = build_codex_runtime(session=session, settings=settings)
    try:
        message = answer_board_question_with_codex(
            runtime=runtime,
            question=question,
            project_keys=[str(key).strip().upper() for key in tenant.jira_config.get("project_keys", []) if str(key).strip()],
            issues=issues,
            status_counts=status_counts,
        )
    except CodexRuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Codex board assistant is unavailable: {exc}",
        ) from exc

    return (
        message,
        {
            "issue_key": normalized_issue_key,
            "status": requested_status,
            "status_counts": status_counts,
            "issues": issues,
            "question": question,
        },
    )


@router.post("/discord/command/{tenant_id}", response_model=DiscordCommandResponse)
def execute_discord_command(
    tenant_id: str,
    payload: DiscordCommandRequest,
    session: Session = Depends(get_session),
) -> DiscordCommandResponse:
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown tenant")
    if not tenant.is_enabled:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Tenant is disabled")

    _assert_channel_scope(tenant=tenant, channel_id=payload.channel_id)
    command_name, arguments = _parse_command_text(payload.command)
    _assert_sensitive_command_permission(tenant=tenant, command_name=command_name, user_id=payload.user_id)

    if command_name == "help":
        return DiscordCommandResponse(ok=True, command=command_name, message=_command_help_message(), data=None)

    if command_name == "policy":
        return DiscordCommandResponse(ok=True, command=command_name, message=_command_policy_message(), data=None)

    if command_name == "status":
        queued_count = int(
            session.execute(
                select(func.count(Run.run_id)).where(
                    Run.tenant_id == tenant_id,
                    Run.status == RUN_STATUS_QUEUED,
                )
            ).scalar_one()
        )
        active_runs = session.execute(
            select(Run)
            .where(
                Run.tenant_id == tenant_id,
                Run.status == RUN_STATUS_RUNNING,
            )
            .order_by(Run.started_at.asc())
        ).scalars().all()
        last_webhook_seen = session.execute(
            select(func.max(WebhookDelivery.created_at)).where(WebhookDelivery.tenant_id == tenant_id)
        ).scalar_one()
        active_payload = [
            {
                "run_id": run.run_id,
                "issue_key": run.issue_key,
                "status": run.status,
                "elapsed_seconds": _format_elapsed_seconds(
                    started_at=run.started_at,
                    created_at=run.created_at,
                ),
            }
            for run in active_runs
        ]
        message = (
            f"Tenant {'enabled' if tenant.is_enabled else 'disabled'}; "
            f"queue_depth={queued_count}; active_runs={len(active_payload)}"
        )
        return DiscordCommandResponse(
            ok=True,
            command=command_name,
            message=message,
            data={
                "webhook_last_seen": last_webhook_seen.isoformat() if last_webhook_seen else None,
                "queue_depth": queued_count,
                "active_runs": active_payload,
            },
        )

    if command_name == "runs":
        limit = 10
        if arguments:
            try:
                limit = min(max(1, int(arguments[0])), 50)
            except ValueError as exc:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid run limit") from exc

        runs = session.execute(
            select(Run)
            .where(Run.tenant_id == tenant_id)
            .order_by(Run.created_at.desc())
            .limit(limit)
        ).scalars().all()
        run_payload = [
            {
                "run_id": run.run_id,
                "issue_key": run.issue_key,
                "status": run.status,
                "pr_url": run.pr_url,
                "created_at": run.created_at.isoformat() if run.created_at else None,
            }
            for run in runs
        ]
        return DiscordCommandResponse(
            ok=True,
            command=command_name,
            message=f"Returned {len(run_payload)} run(s)",
            data={"runs": run_payload},
        )

    if command_name == "link":
        if len(arguments) != 1:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Usage: !link <ISSUE_KEY>")
        issue_key = arguments[0].strip().upper()
        latest_pr = session.execute(
            select(Run)
            .where(
                Run.tenant_id == tenant_id,
                Run.issue_key == issue_key,
                Run.pr_url.is_not(None),
            )
            .order_by(Run.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        jira_base = "https://example.atlassian.net"
        jira_link = f"{jira_base}/browse/{issue_key}"
        return DiscordCommandResponse(
            ok=True,
            command=command_name,
            message=f"Links for {issue_key}",
            data={
                "issue_key": issue_key,
                "jira_url": jira_link,
                "pr_url": latest_pr.pr_url if latest_pr else None,
            },
        )

    if command_name == "ask":
        if not arguments:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Usage: !ask <question> or !ask @ISSUE-123 <question>",
            )
        scoped_issue_key: str | None = None
        question_tokens = arguments
        first_token = arguments[0].strip()
        if first_token.startswith("@"):
            candidate_issue_key = first_token[1:].strip().upper()
            if not ISSUE_KEY_PATTERN.match(candidate_issue_key):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Usage: !ask @ISSUE-123 <question>",
                )
            scoped_issue_key = candidate_issue_key
            question_tokens = arguments[1:]
        question = " ".join(question_tokens).strip()
        if not question:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Usage: !ask <question> or !ask @ISSUE-123 <question>",
            )
        message, data = _ask_board_message(
            session=session,
            tenant=tenant,
            question=question,
            scoped_issue_key=scoped_issue_key,
        )
        return DiscordCommandResponse(
            ok=True,
            command=command_name,
            message=message,
            data=data,
        )

    if command_name == "issues":
        if len(arguments) < 2 or arguments[0].strip().lower() != "seed":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Usage: !issues seed <markdown spec>",
            )
        prompt_markdown = " ".join(arguments[1:]).strip()
        if not prompt_markdown:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Usage: !issues seed <markdown spec>",
            )
        message, data = _seed_issues_with_codex(
            session=session,
            tenant=tenant,
            prompt_markdown=prompt_markdown,
        )
        return DiscordCommandResponse(
            ok=True,
            command=command_name,
            message=message,
            data=data,
        )

    if command_name == "allowlist":
        if not arguments or arguments[0].strip().lower() != "request":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Usage: !allowlist request [reason]",
            )
        reason = " ".join(arguments[1:]).strip() or None
        _, message = _create_allowlist_request(
            session=session,
            tenant=tenant,
            user_id=payload.user_id.strip(),
            channel_id=payload.channel_id.strip() if payload.channel_id else None,
            reason=reason,
        )
        return DiscordCommandResponse(
            ok=True,
            command=command_name,
            message=message,
            data={"user_id": payload.user_id.strip(), "requested": True},
        )

    if command_name == "run":
        if len(arguments) != 1:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Usage: !run <ISSUE_KEY>")
        issue_key = arguments[0].strip().upper()
        issue_preview = _fetch_jira_issue_preview(session=session, tenant=tenant, issue_key=issue_key)
        _ensure_issue_is_executable(issue_status=issue_preview.status, tenant=tenant)
        enqueue_result = enqueue_run(
            session,
            tenant_id=tenant_id,
            issue_key=issue_key,
            issue_summary=issue_preview.summary,
            issue_description=None,
            delivery_id=None,
            max_concurrent_runs=tenant.policy_config.get("max_concurrent_runs"),
        )
        if not enqueue_result.enqueued:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Run could not be queued: {enqueue_result.reason}",
            )
        return DiscordCommandResponse(
            ok=True,
            command=command_name,
            message=f"Queued run {enqueue_result.run.run_id} for {issue_key}",
            data={"run_id": enqueue_result.run.run_id, "issue_key": issue_key},
        )

    if command_name == "cancel":
        if len(arguments) != 1:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Usage: !cancel <RUN_ID>")
        run_id = arguments[0].strip()
        run = session.get(Run, run_id)
        if run is None or run.tenant_id != tenant_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Run {run_id} was not found")
        cancelled = cancel_run(session, run_id=run_id, cancelled_by=payload.user_id)
        return DiscordCommandResponse(
            ok=True,
            command=command_name,
            message=f"Cancelled run {cancelled.run_id}",
            data={"run_id": cancelled.run_id, "status": cancelled.status},
        )

    if command_name == "retry":
        if len(arguments) != 1:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Usage: !retry <ISSUE_KEY|RUN_ID>")
        target = arguments[0].strip()
        run = session.get(Run, target)
        if run is None:
            issue_key = target.upper()
            run = session.execute(
                select(Run)
                .where(Run.tenant_id == tenant_id, Run.issue_key == issue_key)
                .order_by(Run.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()
        if run is None or run.tenant_id != tenant_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No run was found for '{target}'",
            )
        if run.status not in RETRYABLE_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Run {run.run_id} is {run.status}; only failed/blocked/cancelled runs can be retried",
            )
        issue_preview = _fetch_jira_issue_preview(session=session, tenant=tenant, issue_key=run.issue_key)
        _ensure_issue_is_executable(issue_status=issue_preview.status, tenant=tenant)
        enqueue_result = enqueue_run(
            session,
            tenant_id=tenant_id,
            issue_key=run.issue_key,
            issue_summary=issue_preview.summary,
            issue_description=run.issue_description,
            delivery_id=None,
            max_concurrent_runs=tenant.policy_config.get("max_concurrent_runs"),
        )
        if not enqueue_result.enqueued:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Retry could not be queued: {enqueue_result.reason}",
            )
        return DiscordCommandResponse(
            ok=True,
            command=command_name,
            message=f"Queued retry run {enqueue_result.run.run_id} for {run.issue_key}",
            data={"run_id": enqueue_result.run.run_id, "issue_key": run.issue_key},
        )

    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported command")
