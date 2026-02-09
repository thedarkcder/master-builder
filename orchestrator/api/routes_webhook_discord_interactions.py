from __future__ import annotations

import asyncio
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from orchestrator.api.dependencies import get_session
from orchestrator.api.routes_webhook import (
    ASK_REPLY_OPEN_CUSTOM_ID,
    _discord_autocomplete_response,
    _discord_interaction_deferred_response,
    _discord_interaction_modal_response,
    _discord_interaction_response,
    _discord_issue_autocomplete_choices,
    _discord_modal_text_value,
    _find_focused_discord_option,
    _find_tenant_for_discord_channel,
    _parse_ask_confirmation_custom_id,
    _parse_ask_reply_modal_custom_id,
    _parse_discord_interaction_command,
    _read_json_payload,
    _resolve_discord_interactions_public_key,
    _run_discord_ask_confirmation_followup,
    _run_discord_command_followup,
    _validate_discord_interaction_signature,
)
from orchestrator.core.config import get_settings

router = APIRouter(tags=["discord-interactions"])


@router.post("/discord/interactions")
async def ingest_discord_interaction(
    request: Request,
    session: Session = Depends(get_session),
) -> JSONResponse:
    settings = get_settings()
    request_id = request.headers.get("X-Request-Id") or str(uuid4())

    payload, payload_bytes = await _read_json_payload(request, request_id=request_id, source="discord")
    public_key = _resolve_discord_interactions_public_key(session=session, settings=settings)
    _validate_discord_interaction_signature(
        request=request,
        payload_bytes=payload_bytes,
        public_key=public_key,
    )

    interaction_type = payload.get("type")
    if interaction_type == 1:  # PING
        return JSONResponse(status_code=status.HTTP_200_OK, content={"type": 1})

    if interaction_type == 4:  # APPLICATION_COMMAND_AUTOCOMPLETE
        channel_id = payload.get("channel_id")
        if not isinstance(channel_id, str) or not channel_id.strip():
            return _discord_autocomplete_response(choices=[])
        tenant = _find_tenant_for_discord_channel(session=session, channel_id=channel_id.strip())
        if tenant is None:
            return _discord_autocomplete_response(choices=[])

        data = payload.get("data")
        if not isinstance(data, dict):
            return _discord_autocomplete_response(choices=[])
        command_name = str(data.get("name") or "").strip().lower()
        focused = _find_focused_discord_option(data.get("options"))
        if focused is None:
            return _discord_autocomplete_response(choices=[])
        focused_name, focused_value = focused
        supports_issue_autocomplete = (
            (command_name in {"run", "link", "gap"} and focused_name == "issue_key")
            or (command_name in {"retry"} and focused_name == "target")
            or (command_name in {"ask"} and focused_name == "issue_key")
        )
        if not supports_issue_autocomplete:
            return _discord_autocomplete_response(choices=[])
        try:
            choices = _discord_issue_autocomplete_choices(
                session=session,
                tenant=tenant,
                current_value=focused_value,
            )
        except HTTPException:
            choices = []
        return _discord_autocomplete_response(choices=choices)

    if interaction_type == 3:  # MESSAGE_COMPONENT
        channel_id = payload.get("channel_id")
        if not isinstance(channel_id, str) or not channel_id.strip():
            return _discord_interaction_response(content="Missing interaction channel_id", ephemeral=True)
        tenant = _find_tenant_for_discord_channel(session=session, channel_id=channel_id.strip())
        if tenant is None:
            return _discord_interaction_response(
                content="No enabled tenant is configured for this Discord channel.",
                ephemeral=True,
            )

        application_id = str(payload.get("application_id") or "").strip()
        interaction_token = str(payload.get("token") or "").strip()
        if not application_id or not interaction_token:
            return _discord_interaction_response(
                content="Missing Discord interaction context for deferred response.",
                ephemeral=True,
            )

        component_data = payload.get("data")
        if not isinstance(component_data, dict):
            return _discord_interaction_response(content="Missing component interaction data", ephemeral=True)
        custom_id = str(component_data.get("custom_id") or "").strip()
        if custom_id == ASK_REPLY_OPEN_CUSTOM_ID:
            message = payload.get("message")
            message_id = str(message.get("id") or "").strip() if isinstance(message, dict) else ""
            if not message_id:
                return _discord_interaction_response(
                    content="Unable to open reply form because message context is missing.",
                    ephemeral=True,
                )
            return _discord_interaction_modal_response(
                custom_id=f"ask.reply.{message_id}",
                title="Reply to Master Builder",
                text_input_custom_id="question",
                text_input_label="What should I do next?",
                placeholder="Ask a follow-up question or request the next action.",
            )

        parsed_custom_id = _parse_ask_confirmation_custom_id(custom_id)
        if parsed_custom_id is None:
            return _discord_interaction_response(content="Unsupported interaction action", ephemeral=True)
        decision, action_request_id = parsed_custom_id

        user_id = None
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
            return _discord_interaction_response(content="Missing interaction user_id", ephemeral=True)

        asyncio.create_task(
            _run_discord_ask_confirmation_followup(
                tenant_id=tenant.tenant_id,
                user_id=user_id,
                channel_id=channel_id.strip(),
                decision=decision,
                request_id=action_request_id,
                application_id=application_id,
                interaction_token=interaction_token,
            )
        )
        return _discord_interaction_deferred_response(ephemeral=True)

    if interaction_type == 5:  # MODAL_SUBMIT
        channel_id = payload.get("channel_id")
        if not isinstance(channel_id, str) or not channel_id.strip():
            return _discord_interaction_response(content="Missing interaction channel_id", ephemeral=True)
        tenant = _find_tenant_for_discord_channel(session=session, channel_id=channel_id.strip())
        if tenant is None:
            return _discord_interaction_response(
                content="No enabled tenant is configured for this Discord channel.",
                ephemeral=True,
            )

        application_id = str(payload.get("application_id") or "").strip()
        interaction_token = str(payload.get("token") or "").strip()
        if not application_id or not interaction_token:
            return _discord_interaction_response(
                content="Missing Discord interaction context for deferred response.",
                ephemeral=True,
            )

        data = payload.get("data")
        if not isinstance(data, dict):
            return _discord_interaction_response(content="Missing modal interaction data", ephemeral=True)
        reply_to_message_id = _parse_ask_reply_modal_custom_id(str(data.get("custom_id") or "").strip())
        if not reply_to_message_id:
            return _discord_interaction_response(content="Unsupported modal interaction.", ephemeral=True)

        question = _discord_modal_text_value(payload, custom_id="question")
        if not question:
            return _discord_interaction_response(content="Please provide a follow-up question.", ephemeral=True)

        user_id = None
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
            return _discord_interaction_response(content="Missing interaction user_id", ephemeral=True)

        asyncio.create_task(
            _run_discord_command_followup(
                tenant_id=tenant.tenant_id,
                user_id=user_id,
                channel_id=channel_id.strip(),
                command_text=f"!ask {question}",
                application_id=application_id,
                interaction_token=interaction_token,
                reply_to_message_id=reply_to_message_id,
            )
        )
        return _discord_interaction_deferred_response(ephemeral=True)

    if interaction_type != 2:  # APPLICATION_COMMAND
        return _discord_interaction_response(
            content=f"Unsupported Discord interaction type '{interaction_type}'",
            ephemeral=True,
        )

    data = payload.get("data")
    if isinstance(data, dict) and str(data.get("name") or "").strip().lower() == "reply":
        if data.get("type") != 3:
            return _discord_interaction_response(
                content="Reply is a message command. Use it from the message actions menu.",
                ephemeral=True,
            )
        target_message_id = str(data.get("target_id") or "").strip()
        resolved = data.get("resolved")
        resolved_message = None
        if isinstance(resolved, dict):
            resolved_messages = resolved.get("messages")
            if isinstance(resolved_messages, dict):
                resolved_message = resolved_messages.get(target_message_id)
        if not target_message_id:
            return _discord_interaction_response(content="Reply target message was not provided.", ephemeral=True)

        application_id = str(payload.get("application_id") or "").strip()
        if isinstance(resolved_message, dict) and application_id:
            author = resolved_message.get("author")
            author_id = str(author.get("id") or "").strip() if isinstance(author, dict) else ""
            if author_id and author_id != application_id:
                return _discord_interaction_response(
                    content="Use Reply on a Master Builder message.",
                    ephemeral=True,
                )

        return _discord_interaction_modal_response(
            custom_id=f"ask.reply.{target_message_id}",
            title="Reply to Master Builder",
            text_input_custom_id="question",
            text_input_label="What should I do next?",
            placeholder="Ask a follow-up question or request the next action.",
        )

    try:
        user_id, channel_id, command_text, command_params, attachments = _parse_discord_interaction_command(payload)
    except HTTPException as exc:
        return _discord_interaction_response(content=str(exc.detail), ephemeral=True)

    tenant = _find_tenant_for_discord_channel(session=session, channel_id=channel_id)
    if tenant is None:
        return _discord_interaction_response(
            content="No enabled tenant is configured for this Discord channel.",
            ephemeral=True,
        )

    application_id = str(payload.get("application_id") or "").strip()
    interaction_token = str(payload.get("token") or "").strip()
    if not application_id or not interaction_token:
        return _discord_interaction_response(
            content="Missing Discord interaction context for deferred response.",
            ephemeral=True,
        )

    asyncio.create_task(
        _run_discord_command_followup(
            tenant_id=tenant.tenant_id,
            user_id=user_id,
            channel_id=channel_id,
            command_text=command_text,
            application_id=application_id,
            interaction_token=interaction_token,
            command_params=command_params,
            attachments=attachments,
        )
    )
    return _discord_interaction_deferred_response(ephemeral=True)
