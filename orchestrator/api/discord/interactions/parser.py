from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from orchestrator.api.discord.ask.context import (
    project_filter_jql as _project_filter_jql,
    search_jira_issues_for_tenant as _search_jira_issues_for_tenant,
)
from orchestrator.core.discord.channel_tenant_index import resolve_tenant_for_discord_channel
from orchestrator.storage.models import Tenant


def _find_tenant_for_discord_channel(
    *,
    session: Session,
    channel_id: str,
) -> Tenant | None:
    return resolve_tenant_for_discord_channel(session=session, channel_id=channel_id)


def _flatten_discord_option_values(options: object) -> list[str]:
    if not isinstance(options, list):
        return []
    flattened: list[str] = []
    for option in options:
        if not isinstance(option, dict):
            continue
        option_type = option.get("type")
        if option_type in {1, 2}:  # SUB_COMMAND / SUB_COMMAND_GROUP
            nested_name = option.get("name")
            if isinstance(nested_name, str) and nested_name.strip():
                flattened.append(nested_name.strip())
            flattened.extend(_flatten_discord_option_values(option.get("options")))
            continue
        value = option.get("value")
        if value is None:
            continue
        flattened.append(str(value))
    return flattened


def _find_focused_discord_option(options: object) -> tuple[str, str] | None:
    if not isinstance(options, list):
        return None
    for option in options:
        if not isinstance(option, dict):
            continue
        option_type = option.get("type")
        if option_type in {1, 2}:  # SUB_COMMAND / SUB_COMMAND_GROUP
            focused = _find_focused_discord_option(option.get("options"))
            if focused is not None:
                return focused
            continue
        if option.get("focused") is True:
            name = str(option.get("name") or "").strip()
            value = str(option.get("value") or "").strip()
            if name:
                return name, value
    return None


def _discord_option_value(options: object, *, name: str) -> str | None:
    if not isinstance(options, list):
        return None
    for option in options:
        if not isinstance(option, dict):
            continue
        option_type = option.get("type")
        if option_type in {1, 2}:  # SUB_COMMAND / SUB_COMMAND_GROUP
            nested = _discord_option_value(option.get("options"), name=name)
            if nested is not None:
                return nested
            continue
        option_name = str(option.get("name") or "").strip()
        if option_name != name:
            continue
        value = option.get("value")
        if value is None:
            return None
        return str(value).strip()
    return None


def _discord_option_attachment_ids(options: object) -> list[str]:
    if not isinstance(options, list):
        return []
    attachment_ids: list[str] = []
    for option in options:
        if not isinstance(option, dict):
            continue
        option_type = option.get("type")
        if option_type in {1, 2}:  # SUB_COMMAND / SUB_COMMAND_GROUP
            attachment_ids.extend(_discord_option_attachment_ids(option.get("options")))
            continue
        if option_type != 11:  # ATTACHMENT
            continue
        value = str(option.get("value") or "").strip()
        if value:
            attachment_ids.append(value)
    return attachment_ids


def _discord_resolved_attachments(*, data: dict, attachment_ids: list[str]) -> list[dict[str, str]]:
    if not attachment_ids:
        return []
    resolved = data.get("resolved")
    if not isinstance(resolved, dict):
        return []
    resolved_attachments = resolved.get("attachments")
    if not isinstance(resolved_attachments, dict):
        return []
    normalized: list[dict[str, str]] = []
    for attachment_id in attachment_ids:
        item = resolved_attachments.get(attachment_id)
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        proxy_url = str(item.get("proxy_url") or "").strip()
        if not url and not proxy_url:
            continue
        normalized.append(
            {
                "id": str(item.get("id") or "").strip() or attachment_id,
                "url": url or proxy_url,
                "filename": str(item.get("filename") or "").strip(),
                "content_type": str(item.get("content_type") or "").strip(),
                "size": str(item.get("size") or "").strip(),
                "proxy_url": proxy_url,
            }
        )
    return normalized[:5]


def _discord_issue_autocomplete_choices(
    *,
    session: Session,
    tenant: Tenant,
    channel_id: str | None,
    current_value: str,
) -> list[dict]:
    project_jql = _project_filter_jql(session=session, tenant=tenant, channel_id=channel_id)
    normalized = current_value.strip().upper()
    jql = f"{project_jql} ORDER BY updated DESC"
    issues = _search_jira_issues_for_tenant(
        session=session,
        tenant=tenant,
        jql=jql,
        max_results=100,
    )

    choices: list[dict] = []
    seen_keys: set[str] = set()
    for issue in issues:
        issue_key = str(issue.key or "").strip().upper()
        summary = (issue.summary or "").strip()
        if normalized:
            haystack = f"{issue_key} {summary}".upper()
            if normalized not in haystack:
                continue
        if issue.key in seen_keys:
            continue
        seen_keys.add(issue.key)
        display = f"{issue.key} — {summary}" if summary else issue.key
        choices.append({"name": display[:100], "value": issue.key[:100]})
        if len(choices) >= 25:
            break
    return choices


def _parse_discord_interaction_command(payload: dict) -> tuple[str, str, str, dict[str, str] | None, list[dict[str, str]]]:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing interaction data")

    command_name = data.get("name")
    if not isinstance(command_name, str) or not command_name.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing interaction command name")

    channel_id = payload.get("channel_id")
    if not isinstance(channel_id, str) or not channel_id.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing interaction channel_id")

    user_id: str | None = None
    member = payload.get("member")
    if isinstance(member, dict):
        member_user = member.get("user")
        if isinstance(member_user, dict):
            raw_user_id = member_user.get("id")
            if isinstance(raw_user_id, str) and raw_user_id.strip():
                user_id = raw_user_id.strip()
    if user_id is None:
        direct_user = payload.get("user")
        if isinstance(direct_user, dict):
            raw_user_id = direct_user.get("id")
            if isinstance(raw_user_id, str) and raw_user_id.strip():
                user_id = raw_user_id.strip()
    if user_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing interaction user_id")

    normalized_command = command_name.strip().lower()
    options = data.get("options")
    command_text = f"!{normalized_command}"
    command_params: dict[str, str] | None = None
    attachments: list[dict[str, str]] = []
    if normalized_command == "ask":
        issue_key = _discord_option_value(options, name="issue_key")
        question = _discord_option_value(options, name="question")
        if issue_key:
            command_text = f"{command_text} @{issue_key}"
        if question:
            command_text = f"{command_text} {question}"
    elif normalized_command in {"run", "link", "gap"}:
        issue_key = _discord_option_value(options, name="issue_key")
        if issue_key:
            command_text = f"{command_text} {issue_key}"
            if normalized_command == "gap":
                command_params = {"issue_key": issue_key}
    elif normalized_command == "retry":
        target = _discord_option_value(options, name="target")
        if target:
            command_text = f"{command_text} {target}"
    elif normalized_command == "issues":
        subcommands = options if isinstance(options, list) else []
        for option in subcommands:
            if not isinstance(option, dict) or option.get("type") != 1:
                continue
            subcommand_name = str(option.get("name") or "").strip().lower()
            if not subcommand_name:
                continue
            command_text = f"{command_text} {subcommand_name}"
            spec = _discord_option_value(option.get("options"), name="spec")
            if spec:
                command_text = f"{command_text} {spec}"
            break
    elif normalized_command == "request":
        permission = _discord_option_value(options, name="permission")
        reason = _discord_option_value(options, name="reason")
        if permission:
            command_text = f"{command_text} {permission}"
        if reason:
            command_text = f"{command_text} {reason}"
    elif normalized_command == "bug":
        summary = _discord_option_value(options, name="summary")
        details = _discord_option_value(options, name="details")
        issue_key = _discord_option_value(options, name="issue_key")
        attachment_ids = _discord_option_attachment_ids(options)
        attachments = _discord_resolved_attachments(data=data, attachment_ids=attachment_ids)
        command_params = {}
        if summary:
            command_params["summary"] = summary
            command_text = f"{command_text} {summary}"
        if details:
            command_params["details"] = details
        if issue_key:
            command_params["issue_key"] = issue_key
    else:
        option_values = _flatten_discord_option_values(options)
        if option_values:
            command_text = f"{command_text} {' '.join(option_values)}"

    return user_id, channel_id.strip(), command_text, command_params, attachments
