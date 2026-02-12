from __future__ import annotations

from collections.abc import Callable
from re import Pattern
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from orchestrator.api.schemas import DiscordCommandRequest, DiscordCommandResponse
from orchestrator.core.codex_agents import answer_board_question_with_codex, plan_discord_ask_intent_with_codex
from orchestrator.core.codex_runtime import CodexRuntimeError, build_codex_runtime
from orchestrator.core.config import get_settings
from orchestrator.storage.models import Tenant


def dispatch_ask_command(
    *,
    session: Session,
    tenant: Tenant,
    payload: DiscordCommandRequest,
    command_name: str,
    arguments: list[str],
    normalized_user_id: str,
    normalized_channel_id: str,
    require_ask_confirmation: bool,
    issue_key_pattern: Pattern[str],
    collect_ask_context_with_history_context: Callable[..., Any],
    collect_github_ask_context: Callable[..., Any],
    store_pending_ask_action: Callable[..., Any],
    store_ask_history_entry: Callable[..., Any],
    ask_board_message: Callable[..., Any],
    scoped_project_keys: list[str],
) -> DiscordCommandResponse | None:
    if command_name != "ask":
        return None

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
        if not issue_key_pattern.match(candidate_issue_key):
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

    if require_ask_confirmation:
        (
            normalized_issue_key,
            requested_status,
            issues,
            status_counts,
            history_context,
        ) = collect_ask_context_with_history_context(
            session=session,
            tenant=tenant,
            user_id=normalized_user_id,
            channel_id=normalized_channel_id,
            question=question,
            scoped_issue_key=scoped_issue_key,
        )
        settings = get_settings()
        runtime = build_codex_runtime(session=session, settings=settings)
        github_context = collect_github_ask_context(
            session=session,
            tenant=tenant,
            project_keys=[str(key).strip().upper() for key in scoped_project_keys if str(key).strip()],
        )
        try:
            intent_payload = plan_discord_ask_intent_with_codex(
                runtime=runtime,
                question=question,
                project_keys=[str(key).strip().upper() for key in scoped_project_keys if str(key).strip()],
                issues=issues,
                status_counts=status_counts,
                history=history_context,
                github_context=github_context,
            )
        except CodexRuntimeError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Codex board assistant is unavailable: {exc}",
            ) from exc

        mode = str(intent_payload.get("mode") or "").strip().lower()
        summary = str(intent_payload.get("summary") or "").strip()
        proposed_command = str(intent_payload.get("command") or "").strip()
        if mode == "command" and proposed_command.startswith("!") and not proposed_command.lower().startswith("!ask"):
            pending = store_pending_ask_action(
                session=session,
                tenant=tenant,
                user_id=normalized_user_id,
                channel_id=payload.channel_id,
                question=question,
                summary=summary or "Proposed operational action from /ask",
                proposed_command=proposed_command,
            )
            confirmation_message = summary or "I can run this action for you after approval."
            return DiscordCommandResponse(
                ok=True,
                command=command_name,
                message=confirmation_message,
                data={
                    "requires_confirmation": True,
                    "request_id": pending["request_id"],
                    "proposed_command": proposed_command,
                    "summary": confirmation_message,
                },
            )

        message = answer_board_question_with_codex(
            runtime=runtime,
            question=question,
            project_keys=[str(key).strip().upper() for key in scoped_project_keys if str(key).strip()],
            issues=issues,
            status_counts=status_counts,
            history=history_context,
            github_context=github_context,
        )
        store_ask_history_entry(
            session=session,
            tenant=tenant,
            user_id=normalized_user_id,
            channel_id=normalized_channel_id,
            question=question,
            answer=message,
            issue_key=normalized_issue_key,
            status_name=requested_status,
        )
        return DiscordCommandResponse(
            ok=True,
            command=command_name,
            message=message,
            data={
                "issue_key": normalized_issue_key,
                "status": requested_status,
                "status_counts": status_counts,
                "issues": issues,
                "question": question,
            },
        )

    message, data = ask_board_message(
        session=session,
        tenant=tenant,
        user_id=normalized_user_id,
        channel_id=normalized_channel_id,
        question=question,
        scoped_issue_key=scoped_issue_key,
    )
    return DiscordCommandResponse(
        ok=True,
        command=command_name,
        message=message,
        data=data,
    )
