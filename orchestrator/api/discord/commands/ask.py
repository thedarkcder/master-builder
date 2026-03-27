from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from re import Pattern
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from orchestrator.api.schemas import DiscordCommandRequest, DiscordCommandResponse
from orchestrator.api.discord.shared.room_history import DiscordRoomHistoryService
from orchestrator.core.codex_agents import (
    answer_board_question_with_codex,
    answer_pm_question_with_codex,
    plan_discord_ask_intent_with_codex,
)
from orchestrator.core.codex_invocation import CodexInvocationContext
from orchestrator.core.codex_runtime import CodexRuntimeError, build_codex_runtime as _legacy_build_codex_runtime
from orchestrator.core.agent_runtime_resolver import build_runtime_for_selector
from orchestrator.core.config import get_settings
from orchestrator.core.discord.persona_room import answer_voice_room_turn
from orchestrator.storage.models import Project, Tenant


_room_history_service = DiscordRoomHistoryService()
build_codex_runtime = _legacy_build_codex_runtime


def _normalized_project_keys(project_keys: list[str]) -> list[str]:
    return [str(key).strip().upper() for key in project_keys if str(key).strip()]


def _pm_brief_markdown(
    *,
    question: str,
    message: str,
    brief: dict[str, Any],
) -> str:
    def _line_list(value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    objective = str(brief.get("objective") or "").strip() or "Objective not provided."
    recommendation = str(brief.get("recommendation") or "").strip() or "Recommendation not provided."
    acceptance_criteria = _line_list(brief.get("acceptance_criteria"))
    ui_references = _line_list(brief.get("ui_references"))
    scope_in = _line_list(brief.get("scope_in"))
    scope_out = _line_list(brief.get("scope_out"))
    risks = _line_list(brief.get("risks"))
    open_questions = _line_list(brief.get("open_questions"))
    next_steps = _line_list(brief.get("next_steps"))
    success_outcomes = _line_list(brief.get("success_outcomes"))

    def _section(title: str, lines: list[str]) -> str:
        if not lines:
            return f"## {title}\n- None provided."
        return f"## {title}\n" + "\n".join(f"- {line}" for line in lines)

    sections = [
        "## Approved Product Brief",
        f"- Product request: {question.strip()}",
        f"- PM summary: {message.strip()}",
        "",
        "## Objective",
        f"- {objective}",
        "",
        "## Recommendation",
        f"- {recommendation}",
        "",
        _section("Scope In", scope_in),
        "",
        _section("Scope Out", scope_out),
        "",
        _section("Acceptance Criteria", acceptance_criteria),
        "",
        _section("UI / Design / References", ui_references),
        "",
        _section("Risks", risks),
        "",
        _section("Open Questions", open_questions),
        "",
        _section("Next Steps", next_steps),
        "",
        _section("Success Outcomes", success_outcomes),
        "",
        (
            "## Engineering Task Generation Instruction\n"
            "- Upsert a PM-owned parent Jira issue from this brief.\n"
            "- Then derive engineering child tickets from the parent issue.\n"
            "- Keep technical details in child/linked engineering tasks while preserving this product narrative."
        ),
    ]
    return "\n".join(sections)


def _pm_history_question(*, question: str, approved: bool) -> str:
    prefix = "pm:approve" if approved else "pm"
    compact = " ".join(question.strip().split())
    if len(compact) > 220:
        compact = f"{compact[:217]}..."
    return f"{prefix} {compact}".strip()


def _pm_history_answer(answer: str) -> str:
    compact = " ".join(str(answer).strip().split())
    if len(compact) > 400:
        compact = f"{compact[:397]}..."
    return compact


def _voice_turn_history_question(*, question: str, room_mode: bool) -> str:
    compact = " ".join(question.strip().split())
    if len(compact) > 220:
        compact = f"{compact[:217]}..."
    prefix = "room" if room_mode else "voice"
    return f"{prefix} {compact}".strip()


def _voice_turn_history_answer(*, answer: str, persona_id: str) -> str:
    compact = " ".join(str(answer).strip().split())
    if len(compact) > 380:
        compact = f"{compact[:377]}..."
    prefix = str(persona_id or "pm").strip().lower() or "pm"
    return f"{prefix}: {compact}".strip()


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
    prune_missing_issue_keys_from_ask_history: Callable[..., Any],
    recent_ask_history: Callable[..., Any],
    collect_ask_context_with_history_context: Callable[..., Any],
    collect_github_ask_context: Callable[..., Any],
    store_pending_ask_action: Callable[..., Any],
    store_ask_history_entry: Callable[..., Any],
    ask_board_message: Callable[..., Any],
    seed_issues_with_codex: Callable[..., Any] | None,
    scoped_project_keys: list[str],
    scoped_project_id: str | None,
    codex_working_dir: str,
) -> DiscordCommandResponse | None:
    if command_name not in {"ask", "pm"}:
        return None
    is_pm_mode = command_name == "pm"
    normalized_project_keys = _normalized_project_keys(scoped_project_keys)

    if is_pm_mode:
        if not arguments:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Usage: !pm <product request> or !pm approve <handoff request>",
            )
        command_params = payload.command_params if isinstance(payload.command_params, dict) else {}
        is_voice_room_mode = str(command_params.get("room_mode") or "").strip().lower() in {"1", "true", "yes"}
        is_voice_mode = str(command_params.get("voice_mode") or "").strip().lower() in {"1", "true", "yes"}
        is_routed_voice_mode = is_voice_room_mode or is_voice_mode
        first_token = arguments[0].strip().lower()
        approval_requested = first_token == "approve" and not is_routed_voice_mode
        question_tokens = arguments[1:] if approval_requested else arguments
        question = " ".join(question_tokens).strip()
        if not question:
            detail = (
                "Usage: !pm approve <handoff request>"
                if approval_requested
                else "Usage: !pm <product request>"
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=detail,
            )

        normalized_issue_key, requested_status, issues, status_counts, _history_context = (
            collect_ask_context_with_history_context(
                session=session,
                tenant=tenant,
                user_id=normalized_user_id,
                channel_id=normalized_channel_id,
                question=question,
                scoped_issue_key=None,
            )
        )
        settings = get_settings()
        runtime = build_runtime_for_selector(
            session=session,
            settings=settings,
            tenant_id=tenant.tenant_id,
            project_id=scoped_project_id,
            selector="discord.pm_answer",
            agent_role="pm",
            agent_name="voice_room_pm" if is_routed_voice_mode else "pm_primary",
        )
        github_context = collect_github_ask_context(
            session=session,
            tenant=tenant,
            project_keys=normalized_project_keys,
        )
        scoped_project = session.get(Project, scoped_project_id) if scoped_project_id else None
        project_discord_config = scoped_project.discord_config if scoped_project is not None else {}
        if is_routed_voice_mode:
            linked_text_channel_id = str(command_params.get("linked_text_channel_id") or "").strip() or None
            voice_channel_id = str(command_params.get("voice_channel_id") or "").strip() or None
            room_source_mode = str(command_params.get("room_source") or "text").strip().lower() or "text"
            history_owner = scoped_project if scoped_project is not None else tenant
            room_history = _history_context
            room_id = None
            if is_voice_room_mode:
                room_id = _room_history_service.resolve_room_id(
                    room_id=str(command_params.get("room_id") or "").strip() or None,
                    channel_id=normalized_channel_id,
                    linked_text_channel_id=linked_text_channel_id,
                    voice_channel_id=voice_channel_id,
                )
                room_history = _room_history_service.recent_room_history(
                    discord_config=getattr(history_owner, "discord_config", None),
                    room_id=room_id,
                    channel_id=normalized_channel_id,
                    linked_text_channel_id=linked_text_channel_id,
                    voice_channel_id=voice_channel_id,
                )
            try:
                voice_room_result = answer_voice_room_turn(
                    runtime=runtime,
                    runtime_for_selector=lambda selector: build_runtime_for_selector(
                        session=session,
                        settings=settings,
                        tenant_id=tenant.tenant_id,
                        project_id=scoped_project_id,
                        selector=selector,
                        agent_role="pm" if selector == "discord.voice_room_pm" else None,
                        agent_name="voice_room_pm" if selector == "discord.voice_room_pm" else None,
                    ),
                    transcript=question,
                    project_keys=normalized_project_keys,
                    issues=issues,
                    status_counts=status_counts,
                    invocation_context=CodexInvocationContext(
                        channel="discord",
                        tenant_id=tenant.tenant_id,
                        project_id=scoped_project_id,
                        command="pm",
                        stage="voice-room",
                        working_dir=codex_working_dir,
                        issue_key=normalized_issue_key,
                    ),
                    history=room_history,
                    github_context=github_context,
                    tenant_discord_config=getattr(tenant, "discord_config", None) or {},
                    project_discord_config=project_discord_config or {},
                )
            except CodexRuntimeError as exc:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=f"Codex voice room assistant is unavailable: {exc}",
                ) from exc

            store_ask_history_entry(
                session=session,
                tenant=tenant,
                user_id=normalized_user_id,
                channel_id=normalized_channel_id,
                question=_voice_turn_history_question(question=question, room_mode=is_voice_room_mode),
                answer=_voice_turn_history_answer(
                    answer=voice_room_result.message,
                    persona_id=voice_room_result.persona_id,
                ),
                issue_key=normalized_issue_key,
                status_name=requested_status,
            )
            if is_voice_room_mode:
                updated_room_config, _ = _room_history_service.append_room_history_entry(
                    discord_config=getattr(history_owner, "discord_config", None),
                    room_id=room_id,
                    channel_id=normalized_channel_id,
                    linked_text_channel_id=linked_text_channel_id,
                    voice_channel_id=voice_channel_id,
                    speaker_type="user",
                    source_mode=room_source_mode,
                    text=question,
                    user_id=normalized_user_id,
                    issue_key=normalized_issue_key,
                    status_name=requested_status,
                )
                updated_room_config, _ = _room_history_service.append_room_history_entry(
                    discord_config=updated_room_config,
                    room_id=room_id,
                    channel_id=normalized_channel_id,
                    linked_text_channel_id=linked_text_channel_id,
                    voice_channel_id=voice_channel_id,
                    speaker_type="persona",
                    source_mode=room_source_mode,
                    text=voice_room_result.message,
                    persona_id=voice_room_result.persona_id,
                    issue_key=normalized_issue_key,
                    status_name=requested_status,
                    metadata={
                        "router_confidence": voice_room_result.router_confidence,
                        "router_reason": voice_room_result.router_reason,
                    },
                )
                history_owner.discord_config = dict(updated_room_config)
                if hasattr(history_owner, "updated_at"):
                    history_owner.updated_at = datetime.now(timezone.utc)
                session.commit()
            return DiscordCommandResponse(
                ok=True,
                command="pm",
                message=voice_room_result.message,
                data={
                    "pm_mode": True,
                    "room_mode": is_voice_room_mode,
                    "voice_mode": True,
                    "question": question,
                    "issue_key": normalized_issue_key,
                    "status": requested_status,
                    "status_counts": status_counts,
                    "issues": issues,
                    "brief": voice_room_result.brief,
                    "persona_id": voice_room_result.persona_id,
                    "persona_role": voice_room_result.persona_role,
                    "persona_name": voice_room_result.persona_name,
                    "persona_voice_id": voice_room_result.persona_voice_id,
                    "room_config": voice_room_result.room_config,
                    "router": {
                        "persona": voice_room_result.persona_id,
                        "confidence": voice_room_result.router_confidence,
                        "reason": voice_room_result.router_reason,
                    },
                },
            )
        try:
            pm_payload = answer_pm_question_with_codex(
                runtime=runtime,
                question=question,
                action="approve" if approval_requested else "ask",
                project_keys=normalized_project_keys,
                issues=issues,
                status_counts=status_counts,
                invocation_context=CodexInvocationContext(
                    channel="discord",
                    tenant_id=tenant.tenant_id,
                    project_id=scoped_project_id,
                    command="pm",
                    stage="approve" if approval_requested else "answer",
                    working_dir=codex_working_dir,
                    issue_key=normalized_issue_key,
                ),
                history=_history_context,
                github_context=github_context,
            )
        except CodexRuntimeError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Codex board assistant is unavailable: {exc}",
            ) from exc
        message = str(pm_payload.get("message") or "").strip()
        brief = pm_payload.get("brief")
        if not isinstance(brief, dict):
            brief = {}

        store_ask_history_entry(
            session=session,
            tenant=tenant,
            user_id=normalized_user_id,
            channel_id=normalized_channel_id,
            question=_pm_history_question(question=question, approved=approval_requested),
            answer=_pm_history_answer(message),
            issue_key=normalized_issue_key,
            status_name=requested_status,
        )
        response_data: dict[str, Any] = {
            "pm_mode": True,
            "question": question,
            "issue_key": normalized_issue_key,
            "status": requested_status,
            "status_counts": status_counts,
            "issues": issues,
            "brief": brief,
            "approval_required": not approval_requested,
            "approved": approval_requested,
        }
        if approval_requested:
            handoff_markdown = _pm_brief_markdown(
                question=question,
                message=message,
                brief=brief,
            )
            response_data["product_brief_markdown"] = handoff_markdown
            response_data["technical_handoff_markdown"] = handoff_markdown
            response_data["jira_write_hook"] = {
                "enabled": bool(seed_issues_with_codex is not None),
                "action": "seed_issues_with_codex",
                "status": "pending",
            }
            if seed_issues_with_codex is not None:
                try:
                    seed_message, seed_data = seed_issues_with_codex(
                        session=session,
                        tenant=tenant,
                        prompt_markdown=handoff_markdown,
                        scoped_project_id=scoped_project_id,
                        scoped_project_keys=normalized_project_keys,
                        codex_working_dir=codex_working_dir,
                    )
                    response_data["jira_write_hook"] = {
                        "enabled": True,
                        "action": "seed_issues_with_codex",
                        "status": "completed",
                    }
                    response_data["jira_seed_result"] = seed_data
                    if seed_message:
                        message = f"{message}\n\n{seed_message}"
                except Exception as exc:  # noqa: BLE001
                    response_data["jira_write_hook"] = {
                        "enabled": True,
                        "action": "seed_issues_with_codex",
                        "status": "failed",
                        "error": str(exc),
                    }
                    message = (
                        f"{message}\n\n"
                        "I prepared the technical handoff, but Jira engineering task generation failed. "
                        "You can retry with `!issues seed` using the handoff markdown."
                    )
        else:
            response_data["approve_command"] = "!pm approve <handoff request>"

        return DiscordCommandResponse(
            ok=True,
            command="pm",
            message=message,
            data=response_data,
        )

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
        prune_missing_issue_keys_from_ask_history(
            session=session,
            tenant=tenant,
            user_id=normalized_user_id,
            channel_id=normalized_channel_id,
        )
        history_context = recent_ask_history(
            tenant=tenant,
            user_id=normalized_user_id,
            channel_id=normalized_channel_id,
        )
        if not scoped_issue_key:
            for entry in reversed(history_context):
                candidate_issue_key = str(entry.get("issue_key") or "").strip().upper()
                if candidate_issue_key:
                    scoped_issue_key = candidate_issue_key
                    break
        (
            normalized_issue_key,
            requested_status,
            issues,
            status_counts,
            collected_history_context,
        ) = collect_ask_context_with_history_context(
            session=session,
            tenant=tenant,
            user_id=normalized_user_id,
            channel_id=normalized_channel_id,
            question=question,
            scoped_issue_key=scoped_issue_key,
        )
        if collected_history_context:
            history_context = collected_history_context
        settings = get_settings()
        runtime = build_runtime_for_selector(
            session=session,
            settings=settings,
            tenant_id=tenant.tenant_id,
            project_id=scoped_project_id,
            selector="discord.ask_intent",
        )
        invocation_context = CodexInvocationContext(
            channel="discord",
            tenant_id=tenant.tenant_id,
            project_id=scoped_project_id,
            command="ask",
            stage="intent",
            working_dir=codex_working_dir,
            issue_key=scoped_issue_key,
        )
        github_context = collect_github_ask_context(
            session=session,
            tenant=tenant,
            project_keys=normalized_project_keys,
        )
        try:
            intent_payload = plan_discord_ask_intent_with_codex(
                runtime=runtime,
                question=question,
                project_keys=normalized_project_keys,
                issues=issues,
                status_counts=status_counts,
                invocation_context=invocation_context,
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
            runtime=build_runtime_for_selector(
                session=session,
                settings=settings,
                tenant_id=tenant.tenant_id,
                project_id=scoped_project_id,
                selector="discord.ask_answer",
            ),
            question=question,
            project_keys=normalized_project_keys,
            issues=issues,
            status_counts=status_counts,
            invocation_context=CodexInvocationContext(
                channel="discord",
                tenant_id=tenant.tenant_id,
                project_id=scoped_project_id,
                command="ask",
                stage="answer",
                working_dir=codex_working_dir,
                issue_key=scoped_issue_key,
            ),
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
        scoped_project_id=scoped_project_id,
    )
    return DiscordCommandResponse(
        ok=True,
        command=command_name,
        message=message,
        data=data,
    )
