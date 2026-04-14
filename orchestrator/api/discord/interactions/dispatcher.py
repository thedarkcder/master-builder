from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from fastapi import HTTPException
from fastapi.responses import JSONResponse


@dataclass(frozen=True)
class DiscordInteractionDispatchDeps:
    transport_source: str
    ask_reply_open_custom_id: str
    autocomplete_response: Callable[..., JSONResponse]
    interaction_response: Callable[..., JSONResponse]
    interaction_modal_response: Callable[..., JSONResponse]
    interaction_deferred_response: Callable[..., JSONResponse]
    parse_ask_confirmation_custom_id: Callable[[str], tuple[str, str] | None]
    parse_ask_reply_modal_custom_id: Callable[[str], str]
    discord_modal_text_value: Callable[..., str]
    find_tenant_for_discord_channel: Callable[..., object | None]
    find_focused_discord_option: Callable[[object], tuple[str, str] | None]
    discord_issue_autocomplete_choices: Callable[..., list[dict]]
    decision_gate_issue_for_thread: Callable[..., tuple[str, str] | None]
    run_discord_ask_confirmation_followup: Callable[..., object]
    run_discord_command_followup: Callable[..., object]
    run_discord_decision_gate_reply_followup: Callable[..., object]
    run_discord_application_command_followup: Callable[..., object]
    task_scheduler: Callable[[object], object]
    logger: object


async def dispatch_discord_interaction(
    *,
    payload: dict,
    session,
    request_id: str,
    deps: DiscordInteractionDispatchDeps,
) -> JSONResponse:
    interaction_type = payload.get("type")
    if interaction_type == 1:
        return JSONResponse(status_code=200, content={"type": 1})

    if interaction_type == 4:
        return _handle_autocomplete(payload=payload, session=session, deps=deps)

    if interaction_type == 3:
        return _handle_message_component(payload=payload, deps=deps)

    if interaction_type == 5:
        return _handle_modal_submit(payload=payload, session=session, deps=deps)

    if interaction_type != 2:
        return deps.interaction_response(
            content=f"Unsupported Discord interaction type '{interaction_type}'",
            ephemeral=True,
        )

    return _handle_application_command(payload=payload, request_id=request_id, deps=deps)


def _handle_autocomplete(*, payload: dict, session, deps: DiscordInteractionDispatchDeps) -> JSONResponse:
    channel_id = payload.get("channel_id")
    if not isinstance(channel_id, str) or not channel_id.strip():
        deps.logger.info(
            "discord_autocomplete_empty source=%s reason=missing_channel_id",
            deps.transport_source,
        )
        return deps.autocomplete_response(choices=[])
    normalized_channel_id = channel_id.strip()
    tenant = deps.find_tenant_for_discord_channel(session=session, channel_id=normalized_channel_id)
    if tenant is None:
        deps.logger.info(
            "discord_autocomplete_empty source=%s reason=channel_unmapped channel_id=%s",
            deps.transport_source,
            normalized_channel_id,
        )
        return deps.autocomplete_response(choices=[])

    data = payload.get("data")
    if not isinstance(data, dict):
        deps.logger.info(
            "discord_autocomplete_empty source=%s reason=missing_data channel_id=%s tenant_id=%s",
            deps.transport_source,
            normalized_channel_id,
            tenant.tenant_id,
        )
        return deps.autocomplete_response(choices=[])
    command_name = str(data.get("name") or "").strip().lower()
    focused = deps.find_focused_discord_option(data.get("options"))
    if focused is None:
        deps.logger.info(
            "discord_autocomplete_empty source=%s reason=missing_focused_option channel_id=%s tenant_id=%s command_name=%s",
            deps.transport_source,
            normalized_channel_id,
            tenant.tenant_id,
            command_name,
        )
        return deps.autocomplete_response(choices=[])
    focused_name, focused_value = focused
    supports_issue_autocomplete = (
        (command_name in {"run", "link", "gap", "bug"} and focused_name == "issue_key")
        or (command_name in {"retry"} and focused_name == "target")
        or (command_name in {"ask"} and focused_name == "issue_key")
    )
    if not supports_issue_autocomplete:
        deps.logger.info(
            "discord_autocomplete_empty source=%s reason=unsupported_option channel_id=%s tenant_id=%s command_name=%s focused_name=%s",
            deps.transport_source,
            normalized_channel_id,
            tenant.tenant_id,
            command_name,
            focused_name,
        )
        return deps.autocomplete_response(choices=[])
    try:
        choices = deps.discord_issue_autocomplete_choices(
            session=session,
            tenant=tenant,
            channel_id=normalized_channel_id,
            current_value=focused_value,
        )
    except HTTPException as exc:
        deps.logger.exception(
            "discord_autocomplete_failed source=%s tenant_id=%s channel_id=%s command_name=%s focused_name=%s detail=%s error=%s",
            deps.transport_source,
            tenant.tenant_id,
            normalized_channel_id,
            command_name,
            focused_name,
            exc.detail,
            exc,
        )
        choices = []
    return deps.autocomplete_response(choices=choices)


def _handle_message_component(*, payload: dict, deps: DiscordInteractionDispatchDeps) -> JSONResponse:
    channel_id = payload.get("channel_id")
    if not isinstance(channel_id, str) or not channel_id.strip():
        return deps.interaction_response(content="Missing interaction channel_id", ephemeral=True)

    application_id, interaction_token, missing_context = _interaction_context(
        payload=payload,
        interaction_response_fn=deps.interaction_response,
    )
    if missing_context is not None:
        return missing_context

    component_data = payload.get("data")
    if not isinstance(component_data, dict):
        return deps.interaction_response(content="Missing component interaction data", ephemeral=True)
    custom_id = str(component_data.get("custom_id") or "").strip()
    if custom_id == deps.ask_reply_open_custom_id:
        message = payload.get("message")
        message_id = str(message.get("id") or "").strip() if isinstance(message, dict) else ""
        if not message_id:
            return deps.interaction_response(
                content="Unable to open reply form because message context is missing.",
                ephemeral=True,
            )
        return deps.interaction_modal_response(
            custom_id=f"ask.reply.{message_id}",
            title="Reply to Master Builder",
            text_input_custom_id="question",
            text_input_label="What should I do next?",
            placeholder="Ask a follow-up question or request the next action.",
        )

    parsed_custom_id = deps.parse_ask_confirmation_custom_id(custom_id)
    if parsed_custom_id is None:
        return deps.interaction_response(content="Unsupported interaction action", ephemeral=True)
    decision, action_request_id = parsed_custom_id
    user_id = _interaction_user_id(payload)
    if user_id is None:
        return deps.interaction_response(content="Missing interaction user_id", ephemeral=True)

    deps.task_scheduler(
        deps.run_discord_ask_confirmation_followup(
            tenant_id=None,
            user_id=user_id,
            channel_id=channel_id.strip(),
            decision=decision,
            request_id=action_request_id,
            application_id=application_id,
            interaction_token=interaction_token,
        )
    )
    return deps.interaction_deferred_response(ephemeral=True)


def _handle_modal_submit(*, payload: dict, session, deps: DiscordInteractionDispatchDeps) -> JSONResponse:
    channel_id = payload.get("channel_id")
    if not isinstance(channel_id, str) or not channel_id.strip():
        return deps.interaction_response(content="Missing interaction channel_id", ephemeral=True)

    application_id, interaction_token, missing_context = _interaction_context(
        payload=payload,
        interaction_response_fn=deps.interaction_response,
    )
    if missing_context is not None:
        return missing_context

    data = payload.get("data")
    if not isinstance(data, dict):
        return deps.interaction_response(content="Missing modal interaction data", ephemeral=True)
    reply_to_message_id = deps.parse_ask_reply_modal_custom_id(str(data.get("custom_id") or "").strip())
    if not reply_to_message_id:
        return deps.interaction_response(content="Unsupported modal interaction.", ephemeral=True)

    question = deps.discord_modal_text_value(payload, custom_id="question")
    if not question:
        return deps.interaction_response(content="Please provide a follow-up question.", ephemeral=True)

    user_id = _interaction_user_id(payload)
    if user_id is None:
        return deps.interaction_response(content="Missing interaction user_id", ephemeral=True)

    decision_gate_context = deps.decision_gate_issue_for_thread(session=session, channel_id=channel_id.strip())
    if decision_gate_context is not None:
        _, issue_key = decision_gate_context
        deps.task_scheduler(
            deps.run_discord_decision_gate_reply_followup(
                tenant_id=None,
                user_id=user_id,
                channel_id=channel_id.strip(),
                issue_key=issue_key,
                reply_text=question,
                application_id=application_id,
                interaction_token=interaction_token,
                reply_to_message_id=reply_to_message_id,
            )
        )
    else:
        deps.task_scheduler(
            deps.run_discord_command_followup(
                tenant_id=None,
                user_id=user_id,
                channel_id=channel_id.strip(),
                command_text=f"!ask {question}",
                application_id=application_id,
                interaction_token=interaction_token,
                reply_to_message_id=reply_to_message_id,
            )
        )
    return deps.interaction_deferred_response(ephemeral=True)


def _handle_application_command(*, payload: dict, request_id: str, deps: DiscordInteractionDispatchDeps) -> JSONResponse:
    data = payload.get("data")
    if isinstance(data, dict) and str(data.get("name") or "").strip().lower() == "reply":
        if data.get("type") != 3:
            return deps.interaction_response(
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
            return deps.interaction_response(content="Reply target message was not provided.", ephemeral=True)

        application_id = str(payload.get("application_id") or "").strip()
        if isinstance(resolved_message, dict) and application_id:
            author = resolved_message.get("author")
            author_id = str(author.get("id") or "").strip() if isinstance(author, dict) else ""
            if author_id and author_id != application_id:
                return deps.interaction_response(
                    content="Use Reply on a Master Builder message.",
                    ephemeral=True,
                )

        return deps.interaction_modal_response(
            custom_id=f"ask.reply.{target_message_id}",
            title="Reply to Master Builder",
            text_input_custom_id="question",
            text_input_label="What should I do next?",
            placeholder="Ask a follow-up question or request the next action.",
        )

    _, _, missing_context = _interaction_context(
        payload=payload,
        interaction_response_fn=deps.interaction_response,
    )
    if missing_context is not None:
        return missing_context

    deps.task_scheduler(
        deps.run_discord_application_command_followup(
            payload=payload,
            request_id=request_id,
        )
    )
    return deps.interaction_deferred_response(ephemeral=True)


def _interaction_context(*, payload: dict, interaction_response_fn: Callable[..., JSONResponse]) -> tuple[str, str, JSONResponse | None]:
    application_id = str(payload.get("application_id") or "").strip()
    interaction_token = str(payload.get("token") or "").strip()
    if application_id and interaction_token:
        return application_id, interaction_token, None
    return (
        application_id,
        interaction_token,
        interaction_response_fn(
            content="Missing Discord interaction context for deferred response.",
            ephemeral=True,
        ),
    )


def _interaction_user_id(payload: dict) -> str | None:
    member = payload.get("member")
    if isinstance(member, dict):
        member_user = member.get("user")
        if isinstance(member_user, dict):
            raw_user_id = member_user.get("id")
            if isinstance(raw_user_id, str) and raw_user_id.strip():
                return raw_user_id.strip()
    direct_user = payload.get("user")
    if isinstance(direct_user, dict):
        raw_user_id = direct_user.get("id")
        if isinstance(raw_user_id, str) and raw_user_id.strip():
            return raw_user_id.strip()
    return None
