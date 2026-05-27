from __future__ import annotations

import re
from typing import Pattern

from sqlalchemy.orm import Session

from orchestrator.core.decision.types import JiraConfigKey, tenant_jira_config_text
from orchestrator.core.discord.personas import format_voice_room_persona_label
from orchestrator.storage.models import AtlassianOAuthConnection, Tenant


def build_ask_confirmation_components(request_id: str) -> list[dict]:
    return [
        {
            "type": 1,
            "components": [
                {
                    "type": 2,
                    "style": 3,
                    "label": "Approve",
                    "custom_id": f"ask.approve.{request_id}",
                },
                {
                    "type": 2,
                    "style": 4,
                    "label": "Reject",
                    "custom_id": f"ask.reject.{request_id}",
                },
            ],
        }
    ]


def build_command_followup_message(
    *,
    user_id: str,
    command_response,
    jira_browse_base_url: str | None,
    issue_key_pattern: Pattern[str],
) -> str:  # noqa: ANN001
    response_data = command_response.data if isinstance(command_response.data, dict) else {}
    raw_keys = response_data.get("created_issue_keys")
    created_issue_keys = (
        [str(value).strip().upper() for value in raw_keys if str(value).strip()]
        if isinstance(raw_keys, list)
        else []
    )

    def _issue_link(issue_key: str) -> str:
        if not jira_browse_base_url:
            return issue_key
        return f"[{issue_key}]({jira_browse_base_url}/browse/{issue_key})"

    def _linkify_issue_mentions(text: str) -> str:
        if not jira_browse_base_url:
            return text

        def _replace(match: re.Match[str]) -> str:
            issue_key = match.group(0)
            return _issue_link(issue_key)

        return issue_key_pattern.sub(_replace, text)

    lines: list[str] = [f"<@{user_id}>"]
    command_name = str(command_response.command or "").strip().lower()
    response_message = str(command_response.message or "").strip()
    persona_name = str(response_data.get("persona_name") or "").strip()
    persona_role = str(response_data.get("persona_role") or "").strip()
    persona_id = str(response_data.get("persona_id") or "").strip()
    room_src = str(response_data.get("room_source") or "").strip().lower()
    is_voice_style = bool(response_data.get("room_mode")) or room_src in {"voice_note", "live_voice"}
    if command_name == "issues" and created_issue_keys:
        lines[0] = f"{lines[0]} Issue seeding completed."
    elif command_name == "bug" and created_issue_keys:
        lines[0] = f"{lines[0]} Bug logged."
    elif command_name == "link":
        lines[0] = f"{lines[0]} Here are the links."
    elif command_name in {"run", "retry"}:
        run_id = str(response_data.get("run_id") or "").strip()
        recheck_required = bool(response_data.get("recheck_required"))
        if recheck_required:
            if response_message:
                lines[0] = f"{lines[0]} {_linkify_issue_mentions(response_message)}"
            else:
                lines[0] = f"{lines[0]} Action needed before execution."
        elif run_id:
            lines[0] = f"{lines[0]} Run queued."
        else:
            raise ValueError(f"{command_name} response must include run_id unless recheck_required is true")
    elif response_message:
        if command_name in {"ask", "gap", "pm"}:
            response_message = _linkify_issue_mentions(response_message)
        if is_voice_style:
            persona_label = format_voice_room_persona_label(
                persona_id=persona_id,
                persona_name=persona_name,
                persona_role=persona_role,
            )
            lines[0] = f"{lines[0]} {persona_label}: {response_message}"
        else:
            lines[0] = f"{lines[0]} {response_message}"

    if command_name == "link":
        jira_url = str(response_data.get("jira_url") or "").strip()
        pr_url = str(response_data.get("pr_url") or "").strip()
        issue_key = str(response_data.get("issue_key") or "").strip().upper()
        if jira_url or pr_url or issue_key:
            lines.append("")
            lines.append("Links:")
            if jira_url:
                issue_label = issue_key or "Issue"
                lines.append(f"- Jira: [{issue_label}]({jira_url})")
            if pr_url:
                lines.append(f"- PR: [Open PR]({pr_url})")
            if issue_key and not jira_url:
                lines.append(f"- Issue: {_issue_link(issue_key)}")

    if command_name in {"run", "retry"} and not bool(response_data.get("recheck_required")):
        issue_key = str(response_data.get("issue_key") or "").strip().upper()
        run_id = str(response_data.get("run_id") or "").strip()
        if issue_key or run_id:
            lines.append("")
            lines.append("Queued:")
            if issue_key:
                lines.append(f"- Issue: {_issue_link(issue_key)}")
            if run_id:
                lines.append(f"- Run ID: `{run_id}`")

    if command_name == "runs":
        runs = response_data.get("runs")
        if isinstance(runs, list) and runs:
            lines.append("")
            lines.append("Recent runs:")
            for item in runs[:20]:
                if not isinstance(item, dict):
                    continue
                run_id = str(item.get("run_id") or "").strip()
                issue_key = str(item.get("issue_key") or "").strip().upper()
                status_name = str(item.get("status") or "").strip()
                pr_url = str(item.get("pr_url") or "").strip()
                line_parts: list[str] = []
                if run_id:
                    line_parts.append(f"`{run_id}`")
                if status_name:
                    line_parts.append(status_name)
                if issue_key:
                    line_parts.append(_issue_link(issue_key))
                if pr_url:
                    line_parts.append(f"[PR]({pr_url})")
                if line_parts:
                    lines.append(f"- {' | '.join(line_parts)}")

    if command_name == "status":
        active_runs = response_data.get("active_runs")
        if isinstance(active_runs, list) and active_runs:
            lines.append("")
            lines.append("Active runs:")
            for item in active_runs[:20]:
                if not isinstance(item, dict):
                    continue
                run_id = str(item.get("run_id") or "").strip()
                issue_key = str(item.get("issue_key") or "").strip().upper()
                status_name = str(item.get("status") or "").strip()
                line_parts: list[str] = []
                if run_id:
                    line_parts.append(f"`{run_id}`")
                if status_name:
                    line_parts.append(status_name)
                if issue_key:
                    line_parts.append(_issue_link(issue_key))
                if line_parts:
                    lines.append(f"- {' | '.join(line_parts)}")

    if created_issue_keys:
        lines.append("")
        lines.append("Created issues:")
        for issue_key in created_issue_keys[:20]:
            lines.append(f"- {_issue_link(issue_key)}")
        if len(created_issue_keys) > 20:
            lines.append(f"- ...and {len(created_issue_keys) - 20} more")
    content = "\n".join(lines).strip()
    if len(content) <= 1900:
        return content
    return f"{content[:1897]}..."


def resolve_tenant_jira_browse_base_url(*, session: Session, tenant: Tenant) -> str | None:
    connection_id = tenant_jira_config_text(tenant=tenant, key=JiraConfigKey.CONNECTION_ID)
    if not connection_id:
        return None
    connection = session.get(AtlassianOAuthConnection, connection_id)
    if connection is None:
        return None
    normalized_site_url = str(connection.site_url or "").strip().rstrip("/")
    if not normalized_site_url:
        return None
    return normalized_site_url
