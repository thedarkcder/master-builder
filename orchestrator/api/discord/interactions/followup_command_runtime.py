from __future__ import annotations

import asyncio
import logging

from fastapi import HTTPException

logger = logging.getLogger(__name__)


async def run_discord_command_followup(
    *,
    tenant_id: str | None,
    user_id: str,
    channel_id: str,
    command_text: str,
    application_id: str,
    interaction_token: str,
    run_blocking_followup_fn,
    reply_to_message_id: str | None = None,
    command_params: dict[str, str] | None = None,
    attachments: list[dict[str, str]] | None = None,
) -> None:
    await asyncio.to_thread(
        run_blocking_followup_fn,
        tenant_id=tenant_id,
        user_id=user_id,
        channel_id=channel_id,
        command_text=command_text,
        application_id=application_id,
        interaction_token=interaction_token,
        reply_to_message_id=reply_to_message_id,
        command_params=command_params,
        attachments=attachments,
    )


async def run_discord_application_command_followup(
    *,
    payload: dict,
    request_id: str,
    parse_discord_interaction_command_fn,
    send_interaction_followup_fn,
    run_discord_command_followup_fn,
) -> None:
    application_id = str(payload.get("application_id") or "").strip()
    interaction_token = str(payload.get("token") or "").strip()
    if not application_id or not interaction_token:
        logger.error(
            "discord_interaction_followup_missing_context request_id=%s",
            request_id,
        )
        return

    try:
        user_id, channel_id, command_text, command_params, attachments = parse_discord_interaction_command_fn(payload)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        logger.exception(
            "discord_interaction_parse_failed request_id=%s detail=%s error=%s",
            request_id,
            detail,
            exc,
        )
        try:
            send_interaction_followup_fn(
                application_id=application_id,
                interaction_token=interaction_token,
                content=f"Command failed: {detail}",
                ephemeral=True,
            )
        except Exception as followup_exc:  # pragma: no cover
            logger.exception(
                "discord_interaction_parse_error_followup_failed request_id=%s error=%s",
                request_id,
                followup_exc,
            )
        return

    await run_discord_command_followup_fn(
        tenant_id=None,
        user_id=user_id,
        channel_id=channel_id,
        command_text=command_text,
        application_id=application_id,
        interaction_token=interaction_token,
        command_params=command_params,
        attachments=attachments,
    )


async def run_discord_decision_gate_reply_followup(
    *,
    tenant_id: str | None,
    user_id: str,
    channel_id: str,
    issue_key: str,
    reply_text: str,
    application_id: str,
    interaction_token: str,
    run_discord_command_followup_fn,
    reply_to_message_id: str | None = None,
) -> None:
    await run_discord_command_followup_fn(
        tenant_id=tenant_id,
        user_id=user_id,
        channel_id=channel_id,
        command_text="!reply",
        application_id=application_id,
        interaction_token=interaction_token,
        reply_to_message_id=reply_to_message_id,
        command_params={
            "issue_key": issue_key,
            "reply_text": reply_text,
        },
    )



def run_discord_decision_gate_reply_followup_blocking(
    *,
    tenant_id: str | None,
    user_id: str,
    channel_id: str,
    issue_key: str,
    reply_text: str,
    application_id: str,
    interaction_token: str,
    run_discord_command_followup_blocking_fn,
    reply_to_message_id: str | None = None,
) -> None:
    run_discord_command_followup_blocking_fn(
        tenant_id=tenant_id,
        user_id=user_id,
        channel_id=channel_id,
        command_text="!reply",
        application_id=application_id,
        interaction_token=interaction_token,
        reply_to_message_id=reply_to_message_id,
        command_params={
            "issue_key": issue_key,
            "reply_text": reply_text,
        },
    )



def run_discord_command_followup_blocking(
    *,
    tenant_id: str | None,
    user_id: str,
    channel_id: str,
    command_text: str,
    application_id: str,
    interaction_token: str,
    create_session_factory_fn,
    resolve_tenant_id_for_followup_fn,
    resolve_tenant_for_channel_fn,
    build_followup_service_fn,
    run_async_blocking_fn,
    settings_factory_fn,
    execute_command_ingress,
    command_request_factory,
    build_command_followup_message_fn,
    ask_confirmation_components_fn,
    ask_reply_components_fn,
    send_interaction_followup_fn,
    send_thread_reply_fn,
    send_ask_with_thread_fn,
    send_seed_with_thread_fn,
    consume_pending_ask_action_fn,
    reply_to_message_id: str | None = None,
    command_params: dict[str, str] | None = None,
    attachments: list[dict[str, str]] | None = None,
) -> None:
    session_factory = create_session_factory_fn()
    resolved_tenant_id = resolve_tenant_id_for_followup_fn(
        session_factory=session_factory,
        resolve_tenant_for_channel_fn=resolve_tenant_for_channel_fn,
        tenant_id=tenant_id,
        channel_id=channel_id,
    )
    if not resolved_tenant_id:
        logger.warning(
            "discord_interaction_channel_unmapped channel_id=%s command_text=%s",
            channel_id,
            command_text,
        )
        send_interaction_followup_fn(
            application_id=application_id,
            interaction_token=interaction_token,
            content="No enabled tenant is configured for this Discord channel.",
            ephemeral=True,
            reply_to_message_id=reply_to_message_id,
            channel_id=channel_id,
        )
        return

    service = build_followup_service_fn(
        session_factory=session_factory,
        settings_factory=settings_factory_fn,
        execute_command_ingress=execute_command_ingress,
        command_request_factory=command_request_factory,
        build_command_followup_message=build_command_followup_message_fn,
        ask_confirmation_components=ask_confirmation_components_fn,
        ask_reply_components=ask_reply_components_fn,
        send_interaction_followup=send_interaction_followup_fn,
        send_thread_reply=send_thread_reply_fn,
        send_ask_with_thread=send_ask_with_thread_fn,
        send_seed_with_thread=send_seed_with_thread_fn,
        consume_pending_ask_action=consume_pending_ask_action_fn,
    )
    run_async_blocking_fn(
        lambda: service.run_discord_command_followup(
            tenant_id=resolved_tenant_id,
            user_id=user_id,
            channel_id=channel_id,
            command_text=command_text,
            application_id=application_id,
            interaction_token=interaction_token,
            reply_to_message_id=reply_to_message_id,
            command_params=command_params,
            attachments=attachments,
        )
    )


async def run_discord_ask_confirmation_followup(
    *,
    tenant_id: str | None,
    user_id: str,
    channel_id: str,
    decision: str,
    request_id: str,
    application_id: str,
    interaction_token: str,
    run_discord_ask_confirmation_followup_blocking_fn,
) -> None:
    await asyncio.to_thread(
        run_discord_ask_confirmation_followup_blocking_fn,
        tenant_id=tenant_id,
        user_id=user_id,
        channel_id=channel_id,
        decision=decision,
        request_id=request_id,
        application_id=application_id,
        interaction_token=interaction_token,
    )



def run_discord_ask_confirmation_followup_blocking(
    *,
    tenant_id: str | None,
    user_id: str,
    channel_id: str,
    decision: str,
    request_id: str,
    application_id: str,
    interaction_token: str,
    create_session_factory_fn,
    resolve_tenant_id_for_followup_fn,
    resolve_tenant_for_channel_fn,
    build_followup_service_fn,
    run_async_blocking_fn,
    settings_factory_fn,
    execute_command_ingress,
    command_request_factory,
    build_command_followup_message_fn,
    ask_confirmation_components_fn,
    ask_reply_components_fn,
    send_interaction_followup_fn,
    send_thread_reply_fn,
    send_ask_with_thread_fn,
    send_seed_with_thread_fn,
    consume_pending_ask_action_fn,
) -> None:
    session_factory = create_session_factory_fn()
    resolved_tenant_id = resolve_tenant_id_for_followup_fn(
        session_factory=session_factory,
        resolve_tenant_for_channel_fn=resolve_tenant_for_channel_fn,
        tenant_id=tenant_id,
        channel_id=channel_id,
    )
    if not resolved_tenant_id:
        logger.warning(
            "discord_interaction_channel_unmapped channel_id=%s ask_request_id=%s",
            channel_id,
            request_id,
        )
        send_interaction_followup_fn(
            application_id=application_id,
            interaction_token=interaction_token,
            content="No enabled tenant is configured for this Discord channel.",
            ephemeral=True,
        )
        return

    service = build_followup_service_fn(
        session_factory=session_factory,
        settings_factory=settings_factory_fn,
        execute_command_ingress=execute_command_ingress,
        command_request_factory=command_request_factory,
        build_command_followup_message=build_command_followup_message_fn,
        ask_confirmation_components=ask_confirmation_components_fn,
        ask_reply_components=ask_reply_components_fn,
        send_interaction_followup=send_interaction_followup_fn,
        send_thread_reply=send_thread_reply_fn,
        send_ask_with_thread=send_ask_with_thread_fn,
        send_seed_with_thread=send_seed_with_thread_fn,
        consume_pending_ask_action=consume_pending_ask_action_fn,
    )
    run_async_blocking_fn(
        lambda: service.run_discord_ask_confirmation_followup(
            tenant_id=resolved_tenant_id,
            user_id=user_id,
            channel_id=channel_id,
            decision=decision,
            request_id=request_id,
            application_id=application_id,
            interaction_token=interaction_token,
        )
    )
