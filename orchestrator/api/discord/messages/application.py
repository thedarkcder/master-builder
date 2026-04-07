from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from fastapi import HTTPException

from orchestrator.api.schemas import DiscordCommandRequest
from orchestrator.core.communications import (
    DiscordAskWithThreadAction,
    DiscordChannelMessageAction,
    DiscordThreadReplyAction,
    IngressResult,
    TransportAction,
)
from orchestrator.api.discord.ask.context import tenant_project_keys
from orchestrator.core.followup_context_service import FollowupReaction

_ROOM_VOICE_REPLY_COMMANDS = frozenset(
    {
        "pm",
        "room",
        "ask",
        "architect",
        "engineer",
        "tester",
        "security",
        "reviewer",
        "voice_room_persona",
    }
)


@dataclass(frozen=True)
class DiscordMessageIngressDeps:
    find_tenant_for_channel: object
    resolve_project_for_discord_channel: object
    project_room_channel_ids: object
    room_channel_ids_from_discord_config: object
    is_audio_attachment: object
    transcribe_audio_attachment: object
    load_pending_human_input_request: object
    answer_human_input_request: object
    resume_workflow_from_human_input_answer: object
    resolve_followup_context_match: object
    resolve_followup_context: object
    resolve_followup_reaction: object
    execute_tenant_discord_command: object
    resolve_tenant_jira_browse_base_url: object
    build_command_followup_message: object
    build_ask_confirmation_components: object
    ask_reply_components: object
    issue_key_pattern: object
    room_voice_reply_enabled: object
    build_room_voice_reply_action: object
    emit_hard_error: object
    logger: object
    settings: object
    route_voice_entry: object


def discord_channel_message_action(
    *,
    channel_id: str,
    content: str,
    components: list[dict] | None = None,
) -> DiscordChannelMessageAction:
    return DiscordChannelMessageAction(
        channel_id=channel_id,
        content=content,
        components=components,
    )


def _log_ignored_message(
    *,
    deps: DiscordMessageIngressDeps,
    reason: str,
    payload: dict,
    channel_id: str | None = None,
    user_id: str | None = None,
    content: str | None = None,
) -> None:
    normalized_channel_id = str(channel_id or payload.get("channel_id") or "").strip()
    normalized_user_id = str(user_id or ((payload.get("author") or {}) if isinstance(payload.get("author"), dict) else {}).get("id") or "").strip()
    normalized_content = str(content if content is not None else payload.get("content") or "").strip()
    deps.logger.info(
        "discord_gateway_message_ignored reason=%s message_id=%s channel_id=%s user_id=%s has_content=%s starts_with_bang=%s starts_with_slash=%s attachment_count=%s",
        reason,
        str(payload.get("id") or "").strip(),
        normalized_channel_id,
        normalized_user_id,
        bool(normalized_content),
        normalized_content.startswith("!"),
        normalized_content.startswith("/"),
        len(_normalized_attachments(payload)),
    )


def _root_message_id_from_payload(payload: dict) -> str | None:
    message_reference = payload.get("message_reference")
    if isinstance(message_reference, dict):
        normalized = str(message_reference.get("message_id") or "").strip()
        if normalized:
            return normalized
    referenced_message = payload.get("referenced_message")
    if isinstance(referenced_message, dict):
        normalized = str(referenced_message.get("id") or "").strip()
        if normalized:
            return normalized
    return None


def build_discord_message_ingress_result(
    *,
    payload: dict,
    session,
    deps: DiscordMessageIngressDeps,
) -> IngressResult:
    author = payload.get("author")
    if isinstance(author, dict) and author.get("bot") is True:
        _log_ignored_message(deps=deps, reason="bot_author", payload=payload)
        return IngressResult()

    channel_id = str(payload.get("channel_id") or "").strip()
    if not channel_id:
        _log_ignored_message(deps=deps, reason="missing_channel_id", payload=payload)
        return IngressResult()
    user_id = str((author or {}).get("id") or "").strip()
    if not user_id:
        _log_ignored_message(deps=deps, reason="missing_user_id", payload=payload, channel_id=channel_id)
        return IngressResult()
    content = str(payload.get("content") or "").strip()
    if content.startswith("/"):
        _log_ignored_message(
            deps=deps,
            reason="slash_command_message",
            payload=payload,
            channel_id=channel_id,
            user_id=user_id,
            content=content,
        )
        return IngressResult()

    attachments = _normalized_attachments(payload)
    voice_note_reply_requested = False
    room_source_mode = "text"

    tenant = deps.find_tenant_for_channel(session=session, channel_id=channel_id)
    if tenant is None:
        if content.startswith("!"):
            return IngressResult(
                actions=(
                    discord_channel_message_action(
                        channel_id=channel_id,
                        content=f"<@{user_id}> No enabled tenant is configured for this Discord channel.",
                    ),
                )
            )
        return IngressResult()
    project = deps.resolve_project_for_discord_channel(
        session=session,
        tenant_id=tenant.tenant_id,
        channel_id=channel_id,
    )
    project_id = str(getattr(project, "project_id", "") or "").strip() or None
    message_correlation_id = str(payload.get("id") or "").strip() or None
    room_channel_ids = deps.project_room_channel_ids(
        session=session,
        tenant_id=tenant.tenant_id,
    )
    room_channel_ids.update(
        deps.room_channel_ids_from_discord_config(getattr(tenant, "discord_config", None) or {})
    )

    if not content and len(attachments) == 1 and deps.is_audio_attachment(attachments[0]):
        transcript, error_message = deps.transcribe_audio_attachment(
            attachment=attachments[0],
            correlation_id=message_correlation_id,
            tenant_id=tenant.tenant_id,
            project_id=project_id,
        )
        if transcript:
            content = transcript
            room_source_mode = "voice_note"
            voice_note_reply_requested = True
        else:
            graceful_message = error_message or (
                "I detected an audio attachment but couldn't transcribe it. "
                "Please send text or configure voice transcription."
            )
            return IngressResult(
                actions=(
                    discord_channel_message_action(
                        channel_id=channel_id,
                        content=f"<@{user_id}> {graceful_message}",
                    ),
                )
            )

    if not content:
        _log_ignored_message(
            deps=deps,
            reason="empty_content",
            payload=payload,
            channel_id=channel_id,
            user_id=user_id,
            content=content,
        )
        return IngressResult()

    root_message_id = _root_message_id_from_payload(payload)
    followup_resolution = deps.resolve_followup_context_match(
        session=session,
        tenant_id=tenant.tenant_id,
        channel_id=channel_id,
        root_message_id=root_message_id,
        user_id=user_id,
    )
    if str(getattr(followup_resolution, "status", "") or "") == "ambiguous":
        return IngressResult(
            actions=(
                discord_channel_message_action(
                    channel_id=channel_id,
                    content=(
                        f"<@{user_id}> I found multiple active follow-up contexts for that reply. "
                        "Continue in the correct thread or clean up the stale follow-up first."
                    ),
                ),
            )
    )
    followup_context = getattr(followup_resolution, "context", None)
    followup_context_type = str(getattr(followup_context, "context_type", "") or "").strip().lower()
    voice_note_command_params: dict[str, str] | None = None
    voice_note_routed_command_text: str | None = None
    voice_note_scoped_project_keys: list[str] | None = None
    if voice_note_reply_requested and not content.startswith("!"):
        voice_note_scoped_project_keys = (
            [str(project.jira_project_key)]
            if project is not None and getattr(project, "jira_project_key", None)
            else tenant_project_keys(session=session, tenant=tenant)
        )
        routed = deps.route_voice_entry(
            session=session,
            tenant=tenant,
            user_id=user_id,
            channel_id=channel_id,
            project_id=project_id,
            transcript=content,
            project_keys=voice_note_scoped_project_keys,
            room_channel_ids=frozenset(room_channel_ids),
        )
        lane = str(routed.get("lane") or "ask").strip().lower()
        persona_rid = str(routed.get("persona") or "pm").strip().lower()
        deps.logger.info(
            "discord_voice_note_entry_routed lane=%s persona=%s confidence=%s reason=%s",
            lane,
            persona_rid,
            routed.get("confidence"),
            routed.get("reason"),
        )
        voice_note_command_params = {
            "room_mode": "true",
            "room_source": room_source_mode,
        }
        if lane == "interview":
            voice_note_routed_command_text = f"!pm {content}"
        else:
            voice_note_routed_command_text = f"!ask {content}"
            voice_note_command_params["persona_id"] = persona_rid
    if followup_context_type == "pm_interview" and not content.startswith("!"):
        reaction = FollowupReaction(
            kind="command",
            command_text=f"!pm {content}",
        )
    elif voice_note_routed_command_text is not None:
        reaction = FollowupReaction(
            kind="command",
            command_text=voice_note_routed_command_text,
            command_params=voice_note_command_params,
        )
    else:
        reaction = deps.resolve_followup_reaction(
            raw_text=content,
            source_ref=str(payload.get("id") or "").strip() or None,
            followup_context=followup_context,
            room_mode=(channel_id in room_channel_ids) or voice_note_reply_requested,
            room_source=room_source_mode,
        )
    if reaction is not None and getattr(reaction, "kind", "") == "human_input":
        request = deps.load_pending_human_input_request(
            session=session,
            tenant_id=tenant.tenant_id,
            request_id=str(getattr(reaction, "request_id", "") or "").strip(),
        )
        if request is not None:
            try:
                answered_request = deps.answer_human_input_request(
                    session=session,
                    settings=deps.settings,
                    request=request,
                    reply_text=content,
                    source_ref=str(payload.get("id") or "").strip() or None,
                )
                resumed_run = deps.resume_workflow_from_human_input_answer(
                    session=session,
                    settings=deps.settings,
                    request=answered_request,
                )
                message_content = (
                    f"<@{user_id}> Captured input for `{request.issue_key}` "
                    f"and queued workflow attempt `{resumed_run.run_id}`."
                )
            except Exception as exc:  # noqa: BLE001
                deps.logger.exception(
                    "discord_gateway_human_input_resume_failed tenant_id=%s user_id=%s channel_id=%s request_id=%s error=%s",
                    tenant.tenant_id,
                    user_id,
                    channel_id,
                    request.request_id,
                    exc,
                )
                message_content = f"<@{user_id}> Failed to capture the requested input: {exc}"
            return IngressResult(
                actions=(discord_channel_message_action(channel_id=channel_id, content=message_content),),
            )

    if reaction is None and not content.startswith("!") and not ((channel_id in room_channel_ids) or voice_note_reply_requested):
        _log_ignored_message(
            deps=deps,
            reason="plain_text_without_followup_context",
            payload=payload,
            channel_id=channel_id,
            user_id=user_id,
            content=content,
        )
        return IngressResult()

    command_text = content
    command_params = None
    if reaction is not None and getattr(reaction, "kind", "") == "command":
        command_text = str(getattr(reaction, "command_text", "") or "").strip() or content
        params = getattr(reaction, "command_params", None)
        command_params = dict(params) if isinstance(params, dict) and params else None

    message_content = f"<@{user_id}> Command failed due to an internal error."
    components: list[dict] | None = None
    pm_thread_action: DiscordAskWithThreadAction | DiscordThreadReplyAction | None = None
    should_send_room_voice_reply = False
    room_voice_reply_text: str | None = None
    room_voice_reply_persona_id: str | None = None
    room_voice_reply_persona_name: str | None = None
    room_voice_reply_persona_role: str | None = None
    room_voice_reply_config: dict | None = None
    try:
        command_response = deps.execute_tenant_discord_command(
            tenant_id=tenant.tenant_id,
            payload=DiscordCommandRequest(
                user_id=user_id,
                channel_id=channel_id,
                command=command_text,
                command_params=command_params,
                attachments=attachments,
            ),
            session=session,
        )
        message_content = deps.build_command_followup_message(
            user_id=user_id,
            command_response=command_response,
            jira_browse_base_url=deps.resolve_tenant_jira_browse_base_url(
                session=session,
                tenant=tenant,
            ),
            issue_key_pattern=deps.issue_key_pattern,
        )
        data = command_response.data if isinstance(command_response.data, dict) else {}
        pm_interview_mode = bool(data.get("pm_mode")) and command_response.command == "pm"
        if pm_interview_mode:
            pm_followup_context_type = str(data.get("followup_context_type") or "pm_interview").strip() or "pm_interview"
            if followup_context_type == "pm_interview":
                pm_thread_action = DiscordThreadReplyAction(
                    tenant_id=tenant.tenant_id,
                    channel_id=channel_id,
                    reply_to_message_id=message_correlation_id or channel_id,
                    content=message_content,
                    components=components,
                )
            else:
                pm_thread_action = DiscordAskWithThreadAction(
                    tenant_id=tenant.tenant_id,
                    channel_id=channel_id,
                    user_id=user_id,
                    content=message_content,
                    components=components,
                    issue_key=str(data.get("issue_key") or "").strip() or None,
                    followup_context_type=pm_followup_context_type,
                    request_id=str(data.get("request_id") or "").strip() or None,
                )
        if (
            command_response.command == "ask"
            and bool(data.get("requires_confirmation"))
            and isinstance(data.get("request_id"), str)
        ):
            request_id = str(data.get("request_id") or "").strip()
            if request_id:
                components = deps.build_ask_confirmation_components(request_id)
        elif command_response.command == "reply" and bool(data.get("recheck_required")):
            components = deps.ask_reply_components()
        elif (
            channel_id in room_channel_ids
            and command_response.command in _ROOM_VOICE_REPLY_COMMANDS
            and deps.room_voice_reply_enabled()
        ):
            should_send_room_voice_reply = True
            room_voice_reply_text = str(command_response.message or "").strip() or None
            room_voice_reply_persona_id = str(data.get("persona_id") or "").strip() or None
            room_voice_reply_persona_name = str(data.get("persona_name") or "").strip() or None
            room_voice_reply_persona_role = str(data.get("persona_role") or "").strip() or None
            if room_voice_reply_persona_id is None and command_response.command == "pm":
                room_voice_reply_persona_id = "pm"
            if room_voice_reply_persona_id is None and command_response.command == "ask":
                room_voice_reply_persona_id = "pm"
            if room_voice_reply_persona_name is None and room_voice_reply_persona_id == "pm":
                room_voice_reply_persona_name = "PM"
            room_voice_reply_config = data.get("room_config") if isinstance(data.get("room_config"), dict) else None
        elif (
            voice_note_reply_requested
            and command_response.command in _ROOM_VOICE_REPLY_COMMANDS
            and deps.room_voice_reply_enabled()
        ):
            should_send_room_voice_reply = True
            room_voice_reply_text = str(command_response.message or "").strip() or None
            room_voice_reply_persona_id = str(data.get("persona_id") or "").strip() or None
            room_voice_reply_persona_name = str(data.get("persona_name") or "").strip() or None
            room_voice_reply_persona_role = str(data.get("persona_role") or "").strip() or None
            if room_voice_reply_persona_id is None and command_response.command == "pm":
                room_voice_reply_persona_id = "pm"
            if room_voice_reply_persona_id is None and command_response.command == "ask":
                room_voice_reply_persona_id = "pm"
            if room_voice_reply_persona_name is None and room_voice_reply_persona_id == "pm":
                room_voice_reply_persona_name = "PM"
            room_voice_reply_config = data.get("room_config") if isinstance(data.get("room_config"), dict) else None
    except HTTPException as exc:
        deps.logger.exception(
            "discord_gateway_command_http_error tenant_id=%s user_id=%s channel_id=%s detail=%s error=%s",
            tenant.tenant_id,
            user_id,
            channel_id,
            exc.detail,
            exc,
        )
        message_content = f"<@{user_id}> Command failed: {exc.detail}"
    except Exception as exc:  # noqa: BLE001
        error_ref = uuid4().hex[:8]
        deps.logger.exception(
            "discord_gateway_command_failed tenant_id=%s user_id=%s channel_id=%s error_ref=%s error=%s",
            tenant.tenant_id,
            user_id,
            channel_id,
            error_ref,
            exc,
        )
        deps.emit_hard_error(
            event="discord_gateway_command_failed",
            error_ref=error_ref,
            exc=exc,
            context={
                "tenant_id": tenant.tenant_id,
                "user_id": user_id,
                "channel_id": channel_id,
            },
        )
        message_content = f"<@{user_id}> Command failed due to an internal error. Ref: `{error_ref}`"

    actions: list[TransportAction] = []
    if voice_note_reply_requested and should_send_room_voice_reply and room_voice_reply_text:
        if pm_thread_action is None:
            voice_action, voice_error = deps.build_room_voice_reply_action(
                user_id=user_id,
                channel_id=channel_id,
                text=room_voice_reply_text,
                persona_id=room_voice_reply_persona_id,
                persona_name=room_voice_reply_persona_name,
                persona_role=room_voice_reply_persona_role,
                room_config=room_voice_reply_config,
                content_override=message_content,
                components=components,
                correlation_id=message_correlation_id,
                tenant_id=tenant.tenant_id,
                project_id=project_id,
                fallback_content_on_failure=message_content,
                fallback_components_on_failure=components,
            )
            if voice_action is not None:
                return IngressResult(actions=(voice_action,))
            actions.append(
                discord_channel_message_action(
                    channel_id=channel_id,
                    content=message_content,
                    components=components,
                )
            )
            if voice_error:
                actions.append(
                    discord_channel_message_action(
                        channel_id=channel_id,
                        content=f"<@{user_id}> {voice_error}",
                    )
                )
            return IngressResult(actions=tuple(actions))
        actions.append(pm_thread_action)
        voice_action, voice_error = deps.build_room_voice_reply_action(
            user_id=user_id,
            channel_id=channel_id,
            text=room_voice_reply_text,
            persona_id=room_voice_reply_persona_id,
            persona_name=room_voice_reply_persona_name,
            persona_role=room_voice_reply_persona_role,
            room_config=room_voice_reply_config,
            correlation_id=message_correlation_id,
            tenant_id=tenant.tenant_id,
            project_id=project_id,
            fallback_content_on_failure=message_content,
            fallback_components_on_failure=components,
        )
        if voice_action is not None:
            actions.append(voice_action)
        elif voice_error:
            actions.append(
                discord_channel_message_action(
                    channel_id=channel_id,
                    content=f"<@{user_id}> {voice_error}",
                )
            )
        return IngressResult(actions=tuple(actions))
    if pm_thread_action is not None:
        actions.append(pm_thread_action)
    else:
        actions.append(
            discord_channel_message_action(
                channel_id=channel_id,
                content=message_content,
                components=components,
            )
        )
    if should_send_room_voice_reply and room_voice_reply_text:
        voice_action, voice_error = deps.build_room_voice_reply_action(
            user_id=user_id,
            channel_id=channel_id,
            text=room_voice_reply_text,
            persona_id=room_voice_reply_persona_id,
            persona_name=room_voice_reply_persona_name,
            persona_role=room_voice_reply_persona_role,
            room_config=room_voice_reply_config,
            correlation_id=message_correlation_id,
            tenant_id=tenant.tenant_id,
            project_id=project_id,
        )
        if voice_action is not None:
            actions.append(voice_action)
        elif voice_error:
            actions.append(
                discord_channel_message_action(
                    channel_id=channel_id,
                    content=f"<@{user_id}> {voice_error}",
                )
            )
    return IngressResult(actions=tuple(actions))


def _normalized_attachments(payload: dict) -> list[dict[str, str]]:
    raw_attachments = payload.get("attachments")
    attachments: list[dict[str, str]] = []
    if isinstance(raw_attachments, list):
        for item in raw_attachments:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            attachments.append(
                {
                    "id": str(item.get("id") or "").strip(),
                    "url": url,
                    "filename": str(item.get("filename") or "").strip(),
                    "content_type": str(item.get("content_type") or "").strip(),
                    "size": str(item.get("size") or "").strip(),
                }
            )
    return attachments[:5]
