from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from orchestrator.api.schemas import DiscordCommandRequest, DiscordCommandResponse
from orchestrator.storage.models import Tenant


def dispatch_issues_command(
    *,
    session: Session,
    tenant: Tenant,
    payload: DiscordCommandRequest,
    command_name: str,
    arguments: list[str],
    scoped_project_keys: list[str],
    codex_working_dir: str,
    normalized_user_id: str,
    defer_seed_issues: bool,
    seed_issues_with_codex: Callable[..., Any],
    find_seed_followup_context: Callable[..., Any],
    store_seed_followup_context: Callable[..., Any],
    clear_seed_followup_context: Callable[..., Any],
    validate_seed_followup_context: Callable[..., Any] | None = None,
) -> DiscordCommandResponse | None:
    if command_name != "issues":
        return None

    if not arguments:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Usage: !issues seed <markdown spec> | !issues followup <answers>",
        )
    subcommand = arguments[0].strip().lower()
    if subcommand == "seed":
        prompt_markdown = " ".join(arguments[1:]).strip()
        if not prompt_markdown:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Usage: !issues seed <markdown spec>",
            )
        if defer_seed_issues:
            return DiscordCommandResponse(
                ok=True,
                command=command_name,
                message="Issue seeding started. I will reply in this thread with created issue links when done.",
                data={"deferred": True, "prompt_markdown": prompt_markdown},
            )
        message, data = seed_issues_with_codex(
            session=session,
            tenant=tenant,
            prompt_markdown=prompt_markdown,
            scoped_project_keys=scoped_project_keys,
            codex_working_dir=codex_working_dir,
        )
        if (
            isinstance(payload.channel_id, str)
            and payload.channel_id.strip()
            and bool(data.get("requires_input"))
        ):
            request_id = store_seed_followup_context(
                session=session,
                tenant=tenant,
                request_id=None,
                user_id=normalized_user_id,
                channel_ids=[payload.channel_id.strip()],
                project_key=str(data.get("project_key") or ""),
                issue_keys=[str(value) for value in data.get("all_issue_keys", []) if str(value).strip()],
                questions=[str(value) for value in data.get("questions", []) if str(value).strip()],
                prompt_markdown=str(data.get("prompt_markdown") or prompt_markdown),
            )
            data["followup_request_id"] = request_id
        return DiscordCommandResponse(
            ok=True,
            command=command_name,
            message=message,
            data=data,
        )

    if subcommand == "followup":
        followup_text = " ".join(arguments[1:]).strip()
        if not followup_text:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Usage: !issues followup <answers>",
            )
        if not payload.channel_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Follow-up replies require a Discord channel context",
            )
        context = find_seed_followup_context(
            tenant=tenant,
            channel_id=payload.channel_id,
        )
        if context is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="No pending issue-seed follow-up context was found for this channel",
            )
        if validate_seed_followup_context is not None:
            is_valid, invalid_reason = validate_seed_followup_context(
                session=session,
                tenant=tenant,
                context=context,
            )
            if not is_valid:
                clear_seed_followup_context(
                    session=session,
                    tenant=tenant,
                    request_id=str(context.get("request_id") or ""),
                )
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "No pending issue-seed follow-up context was found for this channel"
                        if not invalid_reason
                        else f"Issue-seed follow-up context expired: {invalid_reason}"
                    ),
                )
        context_user_id = str(context.get("user_id") or "").strip()
        if context_user_id and context_user_id != normalized_user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only the original requester can submit this issue-seed follow-up",
            )
        original_prompt = str(context.get("prompt_markdown") or "").strip()
        context_questions = [
            str(value).strip() for value in context.get("questions", []) if str(value).strip()
        ]
        question_block = (
            "\n".join(f"- {value}" for value in context_questions)
            if context_questions
            else "- No explicit questions were captured."
        )
        followup_prompt = (
            f"{original_prompt}\n\n"
            "Additional clarification answers from follow-up conversation:\n"
            f"{followup_text}\n\n"
            "Outstanding clarification questions were:\n"
            f"{question_block}\n\n"
            "Update existing Jira issues where possible. Do not create duplicates."
        )
        forced_issue_keys = [
            str(value).strip().upper() for value in context.get("issue_keys", []) if str(value).strip()
        ]
        message, data = seed_issues_with_codex(
            session=session,
            tenant=tenant,
            prompt_markdown=followup_prompt,
            force_issue_keys=forced_issue_keys,
            allow_create=False,
            scoped_project_keys=[str(context.get("project_key") or "").strip().upper()]
            if str(context.get("project_key") or "").strip()
            else scoped_project_keys,
            codex_working_dir=codex_working_dir,
        )
        if bool(data.get("requires_input")):
            request_id = store_seed_followup_context(
                session=session,
                tenant=tenant,
                request_id=str(context.get("request_id") or ""),
                user_id=normalized_user_id,
                channel_ids=list(
                    {
                        *(context.get("channel_ids") or []),
                        payload.channel_id,
                    }
                ),
                project_key=str(data.get("project_key") or context.get("project_key") or ""),
                issue_keys=[str(value) for value in data.get("all_issue_keys", []) if str(value).strip()],
                questions=[str(value) for value in data.get("questions", []) if str(value).strip()],
                prompt_markdown=str(data.get("prompt_markdown") or followup_prompt),
            )
            data["followup_request_id"] = request_id
        else:
            clear_seed_followup_context(
                session=session,
                tenant=tenant,
                request_id=str(context.get("request_id") or ""),
            )
        return DiscordCommandResponse(
            ok=True,
            command=command_name,
            message=message,
            data=data,
        )

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Usage: !issues seed <markdown spec> | !issues followup <answers>",
    )
