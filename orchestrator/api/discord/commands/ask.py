from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from re import Pattern
from typing import Any
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from orchestrator.api.schemas import DiscordCommandRequest, DiscordCommandResponse
from orchestrator.api.discord.shared.room_history import DiscordRoomHistoryService
from orchestrator.core.codex_agents import (
    answer_board_question_with_runtime,
    plan_discord_ask_intent_with_codex,
)
from orchestrator.core.runtime_invocation import AgentInvocationContext
from orchestrator.core.codex_runtime import CodexRuntimeError
from orchestrator.core.agent_runtime_resolver import build_runtime_for_selector
from orchestrator.core.config import get_settings
from orchestrator.core.pm_plugin_catalog import plugin_catalog_payload, tool_catalog_payload
from orchestrator.core.pm_tool_executor import execute_pm_tool_calls
from orchestrator.core.pm_interview_service import (
    PM_INTERVIEW_STATUS_PM_COMPLETED,
    PM_INTERVIEW_STATUS_READY_TO_WRITE,
    assess_pm_interview_brief,
    format_pm_interview_question,
    mark_pm_interview_case_completed,
    normalize_pm_interview_evidence,
    plan_pm_interview_with_codex,
    resolve_pm_interview_case,
    upsert_pm_interview_case,
)
from orchestrator.core.discord.personas import VOICE_ROOM_PERSONA_IDS, resolve_voice_room_persona_profile
from orchestrator.core.specialist_planning import (
    SpecialistPlanningRequest,
    build_runtime_seed_planning_package,
    run_specialist_planning_fanout,
)
from orchestrator.core.stage_design_planning import invoke_stage_design_planning_llm
from orchestrator.core.stage_spi_policy import resolve_stage_spi_enabled
from orchestrator.core.stage_plugins import evaluate_stage_plugin
from orchestrator.storage.models import Project, Tenant


_room_history_service = DiscordRoomHistoryService()


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
    user_value = str(brief.get("user_value") or "").strip() or "User value not provided."
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
        "## User / Business Value",
        f"- {user_value}",
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
    ]
    return "\n".join(sections)


def _pm_history_question(*, question: str) -> str:
    compact = " ".join(question.strip().split())
    if len(compact) > 220:
        compact = f"{compact[:217]}..."
    return f"pm {compact}".strip()


def _resolve_pm_project_keys(*, project_keys: list[str], issue_key: str | None) -> list[str]:
    normalized_project_keys = _normalized_project_keys(project_keys)
    if len(normalized_project_keys) == 1:
        return normalized_project_keys
    normalized_issue_key = str(issue_key or "").strip().upper()
    if normalized_issue_key and "-" in normalized_issue_key:
        return [normalized_issue_key.split("-", 1)[0]]
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="PM parent issue creation requires a single mapped project scope or an explicit scoped issue key",
    )


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


def _normalized_voice_ask_persona_id(raw: str | None) -> str:
    pid = str(raw or "").strip().lower()
    if pid in set(VOICE_ROOM_PERSONA_IDS):
        return pid
    return "pm"


def _ask_room_voice_overlay(*, tenant: Tenant, persona_id: str) -> dict[str, Any]:
    normalized = _normalized_voice_ask_persona_id(persona_id)
    profile = resolve_voice_room_persona_profile(
        persona_id=normalized,
        tenant_discord_config=getattr(tenant, "discord_config", None) or {},
        project_discord_config=None,
    )
    return {
        "persona_id": profile.persona_id,
        "persona_name": profile.display_name,
        "persona_role": profile.role_label,
        "persona_voice_id": profile.voice_id,
    }


def _merge_ask_voice_reply_fields(
    data: dict[str, Any],
    *,
    tenant: Tenant,
    command_params: dict[str, Any],
) -> dict[str, Any]:
    room_src = str(command_params.get("room_source") or "").strip().lower()
    room_on = str(command_params.get("room_mode") or "").strip().lower() in {"1", "true", "yes"}
    if room_on and room_src in {"voice_note", "live_voice"}:
        merged = dict(data)
        merged.update(
            _ask_room_voice_overlay(
                tenant=tenant,
                persona_id=str(command_params.get("persona_id") or "").strip(),
            )
        )
        merged["room_mode"] = True
        merged["room_source"] = room_src
        return merged
    return data


def _pm_request_id(*, command_params: dict[str, str]) -> str:
    existing = str(command_params.get("request_id") or "").strip()
    return existing or uuid4().hex


def _extract_pm_interview_evidence(
    *,
    question: str,
    attachments: list[dict[str, str]] | None,
    github_context: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = [
        {
            "evidence_id": uuid4().hex,
            "evidence_type": "stakeholder_answer",
            "summary": question,
            "content": question,
            "metadata": {},
        }
    ]
    for attachment in attachments or []:
        source_ref = str(attachment.get("url") or "").strip() or None
        filename = str(attachment.get("filename") or "").strip() or None
        if not source_ref and not filename:
            continue
        evidence.append(
            {
                "evidence_id": uuid4().hex,
                "evidence_type": "file",
                "source_ref": source_ref,
                "title": filename,
                "summary": filename or source_ref,
                "metadata": {
                    "filename": filename,
                    "content_type": str(attachment.get("content_type") or "").strip() or None,
                },
            }
        )
    for token in question.split():
        normalized = str(token).strip().rstrip(").,!?")
        if normalized.startswith(("http://", "https://")):
            evidence.append(
                {
                    "evidence_id": uuid4().hex,
                    "evidence_type": "link",
                    "source_ref": normalized,
                    "summary": normalized,
                    "metadata": {},
                }
            )
    if isinstance(github_context, dict) and github_context:
        evidence.append(
            {
                "evidence_id": uuid4().hex,
                "evidence_type": "context",
                "summary": "GitHub/product context",
                "metadata": {"context": github_context},
            }
        )
    return evidence


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
    seed_parent_issues_with_runtime: Callable[..., Any] | None,
    seed_issues_with_runtime: Callable[..., Any] | None,
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
                detail="Usage: !pm <product request>",
            )
        command_params = payload.command_params if isinstance(payload.command_params, dict) else {}
        is_voice_room_mode = str(command_params.get("room_mode") or "").strip().lower() in {"1", "true", "yes"}
        room_source_mode = str(command_params.get("room_source") or "text").strip().lower() or "text"
        is_voice_ingress = is_voice_room_mode or room_source_mode in {"voice_note", "live_voice"}
        first_token = arguments[0].strip().lower()
        if first_token == "approve" and not is_voice_ingress:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Usage: !pm <product request>",
            )
        question_tokens = arguments
        question = " ".join(question_tokens).strip()
        if not question:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Usage: !pm <product request>",
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
            agent_name="voice_room_pm" if is_voice_ingress else "pm_primary",
        )
        github_context = collect_github_ask_context(
            session=session,
            tenant=tenant,
            project_keys=normalized_project_keys,
        )
        scoped_project = session.get(Project, scoped_project_id) if scoped_project_id else None
        linked_text_channel_id = str(command_params.get("linked_text_channel_id") or "").strip() or None
        voice_channel_id = str(command_params.get("voice_channel_id") or "").strip() or None
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

        request_id = _pm_request_id(command_params=command_params)
        existing_case = resolve_pm_interview_case(
            session=session,
            tenant_id=tenant.tenant_id,
            request_id=request_id,
        )
        existing_brief = getattr(existing_case, "brief_json", None) or {}
        existing_evidence = list(getattr(existing_case, "evidence_json", None) or [])
        current_assessment = assess_pm_interview_brief(
            brief=existing_brief,
            evidence=existing_evidence,
            status_hint=str(getattr(existing_case, "status", "") or "").strip() or None,
        )
        new_evidence = normalize_pm_interview_evidence(
            _extract_pm_interview_evidence(
                question=question,
                attachments=payload.attachments,
                github_context=github_context,
            )
        )
        try:
            pm_payload = plan_pm_interview_with_codex(
                runtime=runtime,
                request_text=question,
                brief=existing_brief,
                evidence=[*existing_evidence, *new_evidence],
                missing_slots=current_assessment.missing_slots,
                next_question=current_assessment.next_question,
                project_keys=normalized_project_keys,
                issues=issues,
                status_counts=status_counts,
                invocation_context=AgentInvocationContext(
                    channel="discord",
                    tenant_id=tenant.tenant_id,
                    project_id=scoped_project_id,
                    command="pm",
                    stage="interview",
                    working_dir=codex_working_dir,
                    issue_key=normalized_issue_key,
                ),
                history=room_history if is_voice_ingress else _history_context,
                github_context=github_context,
                sqlalchemy_session=session,
                settings=settings,
            )
        except CodexRuntimeError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Codex PM interview assistant is unavailable: {exc}",
            ) from exc

        brief = pm_payload.get("brief")
        if not isinstance(brief, dict):
            brief = {}
        message = str(pm_payload.get("message") or "").strip()
        final_assessment = assess_pm_interview_brief(
            brief=brief,
            evidence=[*existing_evidence, *new_evidence],
            status_hint=str(pm_payload.get("status") or "").strip() or None,
        )
        question_history = [
            {
                "speaker": "user",
                "text": question,
                "request_id": request_id,
            },
            {
                "speaker": "pm",
                "text": message,
                "request_id": request_id,
                "status": final_assessment.status,
            },
        ]
        interview_case = upsert_pm_interview_case(
            session=session,
            tenant_id=tenant.tenant_id,
            project_id=scoped_project_id,
            request_id=request_id,
            source_kind="voice_note" if room_source_mode == "voice_note" else "command",
            channel_id=normalized_channel_id,
            thread_channel_id=normalized_channel_id if str(command_params.get("request_id") or "").strip() else None,
            root_message_id=str(command_params.get("root_message_id") or "").strip() or None,
            owner_user_id=normalized_user_id,
            parent_issue_key=str(getattr(existing_case, "parent_issue_key", "") or "").strip() or None,
            source_text=question,
            status=final_assessment.status,
            brief=final_assessment.brief.to_payload(),
            evidence=[item.to_payload() for item in new_evidence],
            question_history=question_history,
            current_question=final_assessment.next_question,
            next_question=final_assessment.next_question,
            notes={"room_source": room_source_mode} if is_voice_ingress else {},
        )

        store_ask_history_entry(
            session=session,
            tenant=tenant,
            user_id=normalized_user_id,
            channel_id=normalized_channel_id,
            question=(
                _voice_turn_history_question(question=question, room_mode=is_voice_room_mode)
                if is_voice_ingress
                else _pm_history_question(question=question)
            ),
            answer=(
                _voice_turn_history_answer(answer=message, persona_id="pm")
                if is_voice_ingress
                else _pm_history_answer(message)
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
                text=message,
                persona_id="pm",
                issue_key=normalized_issue_key,
                status_name=requested_status,
            )
            history_owner.discord_config = dict(updated_room_config)
            if hasattr(history_owner, "updated_at"):
                history_owner.updated_at = datetime.now(timezone.utc)

        product_brief_markdown = _pm_brief_markdown(
            question=question,
            message=message,
            brief=final_assessment.brief.to_payload(),
        )
        response_data: dict[str, Any] = {
            "pm_mode": True,
            "followup_context_type": "pm_interview",
            "request_id": request_id,
            "question": question,
            "issue_key": normalized_issue_key,
            "status": requested_status,
            "status_counts": status_counts,
            "issues": issues,
            "brief": final_assessment.brief.to_payload(),
            "product_brief_markdown": product_brief_markdown,
            "interview_status": final_assessment.status,
            "missing_slots": list(final_assessment.missing_slots),
            "next_question": final_assessment.next_question.to_payload() if final_assessment.next_question is not None else None,
            "next_question_examples": list(final_assessment.next_question.examples) if final_assessment.next_question is not None else [],
            "ready_to_write": final_assessment.ready_to_write,
            "persona_id": "pm",
            "persona_role": "Product Manager",
            "persona_name": "PM",
            "room_mode": is_voice_room_mode or is_voice_ingress,
            "room_source": room_source_mode,
            "room_config": getattr(history_owner, "discord_config", None) if is_voice_room_mode else None,
        }
        if not final_assessment.ready_to_write:
            return DiscordCommandResponse(
                ok=True,
                command="pm",
                message=message or format_pm_interview_question(final_assessment.next_question),
                data=response_data,
            )

        stage_spi_enabled = resolve_stage_spi_enabled(
            session=session,
            settings=settings,
            tenant_id=tenant.tenant_id,
            project_id=scoped_project_id,
        )
        if stage_spi_enabled:
            existing_notes = dict(getattr(interview_case, "notes_json", None) or {})
            raw_stage_state = existing_notes.get("stage_spi")
            stage_state = dict(raw_stage_state) if isinstance(raw_stage_state, dict) else None
            llm_plan_payload = None
            stage_tool_outputs_from_executor: list[dict[str, Any]] = []
            decision_state = None
            selected_plugin_id = str(getattr(settings, "stage_spi_default_plugin", None) or "design").strip().lower()
            plugin_catalog = plugin_catalog_payload()
            allowed_plugin_ids = {str(item.get("plugin_id") or "").strip().lower() for item in plugin_catalog}
            tool_catalog = tool_catalog_payload()
            if bool(getattr(settings, "stage_spi_llm_planning_enabled", False)):
                try:
                    llm_plan_payload = invoke_stage_design_planning_llm(
                        session=session,
                        settings=settings,
                        tenant_id=tenant.tenant_id,
                        project_id=scoped_project_id,
                        working_dir=codex_working_dir,
                        issue_key=normalized_issue_key,
                        stage_plugin=selected_plugin_id,
                        stage_status=str((stage_state or {}).get("stage_status") or "stage_planning").strip().lower() or "stage_planning",
                        stakeholder_text=question,
                        assistant_summary=message,
                        stage_artifacts=dict((stage_state or {}).get("stage_artifacts") or {}),
                        stage_open_questions=list((stage_state or {}).get("stage_open_questions") or []),
                        stage_tool_outputs=[dict(x) for x in ((stage_state or {}).get("stage_tool_outputs") or []) if isinstance(x, dict)],
                    )
                except CodexRuntimeError:
                    llm_plan_payload = None
                if isinstance(llm_plan_payload, dict):
                    plugin_candidate = str(llm_plan_payload.get("selected_plugin_id") or "").strip().lower()
                    if plugin_candidate and plugin_candidate in allowed_plugin_ids:
                        selected_plugin_id = plugin_candidate
                    decision_raw = str(llm_plan_payload.get("decision_state") or "").strip().lower()
                    if decision_raw in {"approved", "revisions_required", "pending"}:
                        decision_state = decision_raw
                    stage_tool_outputs_from_executor = execute_pm_tool_calls(
                        tool_calls=llm_plan_payload.get("tool_calls") if isinstance(llm_plan_payload.get("tool_calls"), list) else None,
                        tenant_id=tenant.tenant_id,
                        project_id=scoped_project_id,
                        default_prompt=message,
                    )
            stage_result = evaluate_stage_plugin(
                state=stage_state,
                stakeholder_text=question,
                assistant_summary=message,
                attachments=payload.attachments,
                plugin_id=selected_plugin_id or None,
                tenant_id=tenant.tenant_id,
                project_id=scoped_project_id,
                request_id=request_id,
                llm_plan_payload=llm_plan_payload,
                explicit_tool_outputs=stage_tool_outputs_from_executor,
                decision_state=decision_state,
            )
            existing_notes["stage_spi"] = {
                "stage_plugin": stage_result.stage_plugin,
                "stage_status": stage_result.stage_status,
                "stage_artifacts": dict(stage_result.stage_artifacts),
                "stage_open_questions": list(stage_result.stage_open_questions),
                "stage_feedback_log": [dict(item) for item in stage_result.stage_feedback_log],
                "stage_tool_outputs": [dict(item) for item in stage_result.stage_tool_outputs],
                "stage_ready_for_implementation": stage_result.stage_ready_for_implementation,
            }
            interview_case.notes_json = existing_notes
            response_data.update(
                {
                    "stage_plugin": stage_result.stage_plugin,
                    "stage_status": stage_result.stage_status,
                    "stage_artifacts": dict(stage_result.stage_artifacts),
                    "stage_open_questions": list(stage_result.stage_open_questions),
                    "stage_tool_outputs": [dict(item) for item in stage_result.stage_tool_outputs],
                    "stage_ready_for_implementation": stage_result.stage_ready_for_implementation,
                    "stage_plugin_catalog": plugin_catalog,
                    "stage_tool_catalog": tool_catalog,
                }
            )
            if stage_result.block_reason:
                response_data["stage_block_reason"] = stage_result.block_reason
            if not stage_result.stage_ready_for_implementation:
                interview_case.status = "question_pending"
                interview_case.current_question_json = {
                    "slot_key": "stage_plugin",
                    "question": stage_result.message,
                    "examples": [],
                }
                interview_case.next_question_json = dict(interview_case.current_question_json)
                interview_case.missing_slots_json = ["stage_plugin"]
                interview_case.updated_at = datetime.now(timezone.utc)
                staged_message = message
                if stage_result.message and stage_result.message not in staged_message:
                    staged_message = f"{staged_message}\n\n{stage_result.message}".strip()
                return DiscordCommandResponse(
                    ok=True,
                    command="pm",
                    message=staged_message,
                    data=response_data,
                )

        scoped_pm_project_keys = _resolve_pm_project_keys(
            project_keys=normalized_project_keys,
            issue_key=normalized_issue_key,
        )
        if seed_parent_issues_with_runtime is None:
            return DiscordCommandResponse(
                ok=True,
                command="pm",
                message=message,
                data=response_data,
            )
        parent_seed_message, parent_seed_data = seed_parent_issues_with_runtime(
            session=session,
            tenant=tenant,
            prompt_markdown=product_brief_markdown,
            scoped_project_id=scoped_project_id,
            scoped_project_keys=scoped_pm_project_keys,
            codex_working_dir=codex_working_dir,
            pm_status=PM_INTERVIEW_STATUS_READY_TO_WRITE,
            pm_interview_notes_json=dict(getattr(interview_case, "notes_json", None) or {}),
        )
        parent_issue_key = str(
            (list(parent_seed_data.get("all_parent_issue_keys", [])) or [None])[0] or getattr(interview_case, "parent_issue_key", "") or ""
        ).strip() or None
        response_data.update(
            {
                "parent_issue_key": parent_issue_key,
                "created_parent_issue_keys": list(parent_seed_data.get("created_parent_issue_keys", [])),
                "updated_parent_issue_keys": list(parent_seed_data.get("updated_parent_issue_keys", [])),
                "created_parent_issue_links": list(parent_seed_data.get("created_parent_issue_links", [])),
                "updated_parent_issue_links": list(parent_seed_data.get("updated_parent_issue_links", [])),
                "all_parent_issue_keys": list(parent_seed_data.get("all_parent_issue_keys", [])),
            }
        )

        planning_message = ""
        planning_state = None
        planning_package: dict[str, Any] | None = None
        if parent_issue_key and seed_issues_with_runtime is not None:
            planning_request = SpecialistPlanningRequest(
                tenant_id=tenant.tenant_id,
                project_id=scoped_project_id,
                parent_issue_key=parent_issue_key,
                parent_summary=str(final_assessment.brief.objective or question).strip() or question,
                parent_description=product_brief_markdown,
                product_brief=final_assessment.brief.to_payload(),
                project_keys=tuple(scoped_pm_project_keys),
                related_issues=tuple(issues),
                status_counts=status_counts,
                github_context=github_context,
                conversation_history=tuple(room_history if is_voice_ingress else _history_context),
                working_dir=codex_working_dir,
            )
            planning_result = run_specialist_planning_fanout(
                runtime=runtime,
                request=planning_request,
                runtime_for_selector=lambda selector: build_runtime_for_selector(
                    session=session,
                    settings=settings,
                    tenant_id=tenant.tenant_id,
                    project_id=scoped_project_id,
                    selector=selector,
                ),
            )
            planning_state = planning_result.planning_state
            planning_package = build_runtime_seed_planning_package(
                result=planning_result,
                behavior_slice=str(final_assessment.brief.objective or question).strip() or question,
            )
            issue_seed_message, issue_seed_data = seed_issues_with_runtime(
                session=session,
                tenant=tenant,
                prompt_markdown=product_brief_markdown,
                force_issue_keys=[parent_issue_key],
                allow_create=True,
                scoped_project_id=scoped_project_id,
                scoped_project_keys=scoped_pm_project_keys,
                codex_working_dir=codex_working_dir,
                pm_status=PM_INTERVIEW_STATUS_PM_COMPLETED,
                planning_package=planning_package,
            )
            response_data.update(issue_seed_data)
            response_data["planning_package"] = planning_package
            planning_message = issue_seed_message
            if planning_result.planning_state == "planning_completed":
                mark_pm_interview_case_completed(
                    session=session,
                    tenant_id=tenant.tenant_id,
                    request_id=request_id,
                    parent_issue_key=parent_issue_key,
                    notes={"planning_state": planning_state},
                )
                interview_case.status = PM_INTERVIEW_STATUS_PM_COMPLETED
            elif planning_result.open_behavior_questions:
                interview_case.status = "question_pending"
                interview_case.current_question_json = {
                    "slot_key": "planning",
                    "question": planning_result.open_behavior_questions[0],
                    "examples": [],
                }
                interview_case.next_question_json = dict(interview_case.current_question_json)
                interview_case.missing_slots_json = ["planning"]
                interview_case.updated_at = datetime.now(timezone.utc)

        response_data["planning_state"] = planning_state
        combined_message = message
        if parent_seed_message:
            combined_message = f"{combined_message}\n\n{parent_seed_message}".strip()
        if planning_message:
            combined_message = f"{combined_message}\n\n{planning_message}".strip()
        return DiscordCommandResponse(
            ok=True,
            command="pm",
            message=combined_message,
            data=response_data,
        )

    if not arguments:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Usage: !ask <question> or !ask @ISSUE-123 <question>",
        )
    ask_command_params = payload.command_params if isinstance(payload.command_params, dict) else {}
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
        invocation_context = AgentInvocationContext(
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

        ask_voice_persona = _normalized_voice_ask_persona_id(str(ask_command_params.get("persona_id") or ""))
        message = answer_board_question_with_runtime(
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
            invocation_context=AgentInvocationContext(
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
            answer_persona_id=ask_voice_persona,
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
        ask_data = {
            "issue_key": normalized_issue_key,
            "status": requested_status,
            "status_counts": status_counts,
            "issues": issues,
            "question": question,
        }
        ask_data = _merge_ask_voice_reply_fields(ask_data, tenant=tenant, command_params=ask_command_params)
        return DiscordCommandResponse(
            ok=True,
            command=command_name,
            message=message,
            data=ask_data,
        )

    ask_voice_persona = _normalized_voice_ask_persona_id(str(ask_command_params.get("persona_id") or ""))
    message, data = ask_board_message(
        session=session,
        tenant=tenant,
        user_id=normalized_user_id,
        channel_id=normalized_channel_id,
        question=question,
        scoped_issue_key=scoped_issue_key,
        scoped_project_id=scoped_project_id,
        answer_persona_id=ask_voice_persona,
    )
    if isinstance(data, dict):
        merged_data = _merge_ask_voice_reply_fields(dict(data), tenant=tenant, command_params=ask_command_params)
    else:
        merged_data = data
    return DiscordCommandResponse(
        ok=True,
        command=command_name,
        message=message,
        data=merged_data,
    )
