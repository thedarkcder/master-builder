from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from orchestrator.api.schemas import DiscordCommandRequest, DiscordCommandResponse
from orchestrator.core.runtime.agent_runtime_resolver import build_runtime_for_selector
from orchestrator.core.runtime.agents import answer_voice_room_persona_with_runtime
from orchestrator.core.runtime.invocation import AgentInvocationContext
from orchestrator.core.runtime.runtime import CodexRuntimeError
from orchestrator.core.config import get_settings
from orchestrator.core.discord.personas import resolve_voice_room_persona_profile
from orchestrator.storage.models import Tenant

_PERSONA_COMMAND_TO_ID = {
    "architect": "architect",
    "engineer": "engineer",
    "tester": "qa",
    "security": "security",
    "reviewer": "reviewer",
}


def _normalized_project_keys(project_keys: list[str]) -> list[str]:
    return [str(key).strip().upper() for key in project_keys if str(key).strip()]


def _persona_history_question(*, command_name: str, question: str) -> str:
    compact = " ".join(question.strip().split())
    if len(compact) > 220:
        compact = f"{compact[:217]}..."
    return f"{command_name.strip().lower()} {compact}".strip()


def dispatch_persona_command(
    *,
    session: Session,
    tenant: Tenant,
    payload: DiscordCommandRequest,
    command_name: str,
    arguments: list[str],
    normalized_user_id: str,
    normalized_channel_id: str,
    issue_key_pattern,
    collect_ask_context_with_history_context: Callable[..., Any],
    collect_github_ask_context: Callable[..., Any],
    store_ask_history_entry: Callable[..., Any],
    scoped_project_keys: list[str],
    scoped_project_id: str | None,
    codex_working_dir: str,
) -> DiscordCommandResponse | None:
    persona_id = _PERSONA_COMMAND_TO_ID.get(command_name)
    if persona_id is None:
        return None

    if not arguments:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Usage: !{command_name} <question> or !{command_name} @ISSUE-123 <question>",
        )

    scoped_issue_key: str | None = None
    question_tokens = arguments
    first_token = arguments[0].strip()
    if first_token.startswith("@"):
        candidate_issue_key = first_token[1:].strip().upper()
        if not issue_key_pattern.match(candidate_issue_key):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Usage: !{command_name} @ISSUE-123 <question>",
            )
        scoped_issue_key = candidate_issue_key
        question_tokens = arguments[1:]

    question = " ".join(question_tokens).strip()
    if not question:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Usage: !{command_name} <question> or !{command_name} @ISSUE-123 <question>",
        )

    normalized_project_keys = _normalized_project_keys(scoped_project_keys)
    normalized_issue_key, requested_status, issues, status_counts, history_context = (
        collect_ask_context_with_history_context(
            session=session,
            tenant=tenant,
            user_id=normalized_user_id,
            channel_id=normalized_channel_id,
            question=question,
            scoped_issue_key=scoped_issue_key,
        )
    )
    settings = get_settings()
    runtime = build_runtime_for_selector(
        session=session,
        settings=settings,
        tenant_id=tenant.tenant_id,
        project_id=scoped_project_id,
        selector=f"discord.{command_name}_answer",
    )
    github_context = collect_github_ask_context(
        session=session,
        tenant=tenant,
        project_keys=normalized_project_keys,
    )
    persona_profile = resolve_voice_room_persona_profile(
        persona_id=persona_id,
        tenant_discord_config=getattr(tenant, "discord_config", None) or {},
        project_discord_config=None,
    )
    try:
        answer_payload = answer_voice_room_persona_with_runtime(
            runtime=runtime,
            persona_id=persona_id,
            transcript=question,
            project_keys=normalized_project_keys,
            issues=issues,
            status_counts=status_counts,
            invocation_context=AgentInvocationContext(
                channel="discord",
                tenant_id=tenant.tenant_id,
                project_id=scoped_project_id,
                command=command_name,
                stage=f"{command_name}-answer",
                working_dir=codex_working_dir,
                issue_key=normalized_issue_key,
            ),
            history=history_context,
            github_context=github_context,
            sqlalchemy_session=session,
            settings=settings,
        )
    except CodexRuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Codex {command_name} assistant is unavailable: {exc}",
        ) from exc

    message = str(answer_payload.get("message") or "").strip()
    brief = answer_payload.get("brief")
    if not isinstance(brief, dict):
        brief = {}
    store_ask_history_entry(
        session=session,
        tenant=tenant,
        user_id=normalized_user_id,
        channel_id=normalized_channel_id,
        question=_persona_history_question(
            command_name=command_name, question=question
        ),
        answer=message,
        issue_key=normalized_issue_key,
        status_name=requested_status,
    )
    return DiscordCommandResponse(
        ok=True,
        command=command_name,
        message=message,
        data={
            "question": question,
            "issue_key": normalized_issue_key,
            "status": requested_status,
            "status_counts": status_counts,
            "issues": issues,
            "brief": brief,
            "advisory_only": True,
            "persona_id": persona_profile.persona_id,
            "persona_name": persona_profile.display_name,
            "persona_role": persona_profile.role_label,
            "persona_voice_id": persona_profile.voice_id,
        },
    )
