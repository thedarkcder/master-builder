from __future__ import annotations

from collections.abc import Callable
from re import Pattern
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from orchestrator.api.schemas import DiscordCommandRequest, DiscordCommandResponse
from orchestrator.storage.models import Tenant


def dispatch_bug_gap_command(
    *,
    session: Session,
    tenant: Tenant,
    payload: DiscordCommandRequest,
    command_name: str,
    arguments: list[str],
    scoped_project_id: str | None,
    issue_key_pattern: Pattern[str],
    scoped_project_keys: list[str],
    run_gap_analysis: Callable[..., Any],
    normalize_discord_attachments: Callable[[object], list[dict[str, str]]],
    create_discord_bug_issue: Callable[..., Any],
) -> DiscordCommandResponse | None:
    if command_name == "gap":
        command_params = (
            payload.command_params if isinstance(payload.command_params, dict) else {}
        )
        issue_key_param = str(command_params.get("issue_key") or "").strip().upper()
        issue_key_arg = arguments[0].strip().upper() if arguments else ""
        issue_key = issue_key_param or issue_key_arg
        if not issue_key:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Usage: !gap <ISSUE_KEY>",
            )
        message, data = run_gap_analysis(
            session=session,
            tenant=tenant,
            issue_key=issue_key,
        )
        return DiscordCommandResponse(
            ok=True,
            command=command_name,
            message=message,
            data=data,
        )

    if command_name == "bug":
        command_params = (
            payload.command_params if isinstance(payload.command_params, dict) else {}
        )
        summary = str(command_params.get("summary") or "").strip()
        details = str(command_params.get("details") or "").strip()
        related_issue_key_raw = (
            str(command_params.get("issue_key") or "").strip().upper()
        )
        related_issue_key = (
            related_issue_key_raw
            if issue_key_pattern.match(related_issue_key_raw)
            else None
        )
        if not summary:
            raw_body = " ".join(arguments).strip()
            if " -- " in raw_body:
                summary, details_tail = raw_body.split(" -- ", 1)
                summary = summary.strip()
                if not details:
                    details = details_tail.strip()
            else:
                summary = raw_body
        if not summary:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Usage: !bug <summary> [-- details]",
            )
        attachments = normalize_discord_attachments(payload.attachments)
        message, data = create_discord_bug_issue(
            session=session,
            tenant=tenant,
            summary=summary,
            details=details,
            reporter_user_id=payload.user_id.strip(),
            channel_id=payload.channel_id,
            related_issue_key=related_issue_key,
            attachments=attachments,
            selected_project_key=(
                scoped_project_keys[0]
                if scoped_project_id and scoped_project_keys
                else None
            ),
        )
        return DiscordCommandResponse(
            ok=True,
            command=command_name,
            message=message,
            data=data,
        )

    return None
