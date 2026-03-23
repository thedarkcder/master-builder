from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from fastapi import HTTPException

from orchestrator.api.schemas import DiscordCommandRequest
from orchestrator.core.communications import (
    DiscordChannelMessageAction,
    IngressResult,
    TransportAction,
)


@dataclass(frozen=True)
class DiscordMessageIngressDeps:
    find_tenant_for_channel: object
    resolve_project_for_discord_channel: object
    project_room_channel_ids: object
    room_channel_ids_from_discord_config: object
    is_audio_attachment: object
    transcribe_audio_attachment: object
    pending_human_input_for_thread: object
    resume_run_from_human_input_reply: object
    decision_gate_issue_for_thread: object
    project_seed_followup_thread_ids: object
    project_seed_followup_thread_project_keys: object
    find_seed_followup_context: object
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

    pending_human_input = deps.pending_human_input_for_thread(
        session=session,
        tenant_id=tenant.tenant_id,
        thread_channel_id=channel_id,
    )
    if pending_human_input is not None and not content.startswith("!"):
        try:
            resumed_run = deps.resume_run_from_human_input_reply(
                session=session,
                settings=deps.settings,
                request=pending_human_input,
                reply_text=content,
                source_ref=str(payload.get("id") or "").strip() or None,
            )
            message_content = (
                f"<@{user_id}> Captured input for `{pending_human_input.issue_key}` "
                f"and queued resumed run `{resumed_run.run_id}`."
            )
        except Exception as exc:  # noqa: BLE001
            deps.logger.exception(
                "discord_gateway_human_input_resume_failed tenant_id=%s user_id=%s channel_id=%s request_id=%s error=%s",
                tenant.tenant_id,
                user_id,
                channel_id,
                pending_human_input.request_id,
                exc,
            )
            message_content = f"<@{user_id}> Failed to capture the requested input: {exc}"
        return IngressResult(
            actions=(discord_channel_message_action(channel_id=channel_id, content=message_content),),
        )

    decision_gate_issue_key = deps.decision_gate_issue_for_thread(
        session=session,
        tenant_id=tenant.tenant_id,
        channel_id=channel_id,
    )
    if decision_gate_issue_key and not content.startswith("!"):
        command_text = "!reply"
        command_params = {
            "issue_key": decision_gate_issue_key,
            "reply_text": content,
            "source_ref": str(payload.get("id") or "").strip(),
        }
    else:
        command_text = content
        command_params = None

    seed_followup_thread_ids = deps.project_seed_followup_thread_ids(
        session=session,
        tenant_id=tenant.tenant_id,
    )
    seed_followup_thread_project_keys = deps.project_seed_followup_thread_project_keys(
        session=session,
        tenant_id=tenant.tenant_id,
    )
    seed_followup_context = deps.find_seed_followup_context(
        tenant=tenant,
        channel_id=channel_id,
    )
    if seed_followup_context is None and channel_id in seed_followup_thread_ids:
        seed_followup_context = deps.find_seed_followup_context(
            tenant=tenant,
            channel_id=channel_id,
            user_id=user_id,
            project_key=seed_followup_thread_project_keys.get(channel_id),
        )
    if (
        channel_id in seed_followup_thread_ids
        and seed_followup_context is not None
        and not command_text.startswith("!")
    ):
        command_text = f"!issues followup {command_text}"
    if channel_id in room_channel_ids and not command_text.startswith("!"):
        command_text = f"!pm {command_text}"
        command_params = {
            **(command_params or {}),
            "room_mode": "true",
            "room_source": room_source_mode,
        }
    elif voice_note_reply_requested and not command_text.startswith("!"):
        command_text = f"!pm {command_text}"
        command_params = {
            **(command_params or {}),
            "voice_mode": "true",
            "voice_source": room_source_mode,
        }

    message_content = f"<@{user_id}> Command failed due to an internal error."
    components: list[dict] | None = None
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
            and command_response.command in {"pm", "room"}
            and deps.room_voice_reply_enabled()
        ):
            should_send_room_voice_reply = True
            room_voice_reply_text = str(command_response.message or "").strip() or None
            room_voice_reply_persona_id = str(data.get("persona_id") or "").strip() or None
            room_voice_reply_persona_name = str(data.get("persona_name") or "").strip() or None
            room_voice_reply_persona_role = str(data.get("persona_role") or "").strip() or None
            if room_voice_reply_persona_id is None and command_response.command == "pm":
                room_voice_reply_persona_id = "pm"
            if room_voice_reply_persona_name is None and room_voice_reply_persona_id == "pm":
                room_voice_reply_persona_name = "PM"
            room_voice_reply_config = data.get("room_config") if isinstance(data.get("room_config"), dict) else None
        elif (
            voice_note_reply_requested
            and command_response.command == "pm"
            and deps.room_voice_reply_enabled()
        ):
            should_send_room_voice_reply = True
            room_voice_reply_text = str(command_response.message or "").strip() or None
            room_voice_reply_persona_id = str(data.get("persona_id") or "").strip() or "pm"
            room_voice_reply_persona_name = str(data.get("persona_name") or "").strip() or "PM"
            room_voice_reply_persona_role = str(data.get("persona_role") or "").strip() or None
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
            actions.append(voice_action)
        else:
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
