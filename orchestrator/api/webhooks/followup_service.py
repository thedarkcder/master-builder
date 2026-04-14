from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager

from sqlalchemy.orm import Session

from orchestrator.api.webhooks.followup_execution import (
    DiscordFollowupExecutionDeps,
    run_discord_ask_confirmation_followup as _run_discord_ask_confirmation_followup,
    run_discord_command_followup as _run_discord_command_followup,
)
from orchestrator.core.communications.integration_contracts import TransportActionExecutor


class DiscordWebhookFollowupService:
    def __init__(
        self,
        *,
        session_factory: Callable[[], AbstractContextManager[Session]],
        settings_factory: Callable[[], object],
        execute_command_ingress: Callable[..., object],
        command_request_factory: Callable[..., object],
        build_command_followup_message: Callable[..., str],
        ask_confirmation_components: Callable[[str], list[dict]],
        ask_reply_components: Callable[[], list[dict]],
        transport_executor: TransportActionExecutor,
        consume_pending_ask_action: Callable[..., dict | None],
    ) -> None:
        self._deps = DiscordFollowupExecutionDeps(
            session_factory=session_factory,
            settings_factory=settings_factory,
            execute_command_ingress=execute_command_ingress,
            command_request_factory=command_request_factory,
            build_command_followup_message=build_command_followup_message,
            ask_confirmation_components=ask_confirmation_components,
            ask_reply_components=ask_reply_components,
            transport_executor=transport_executor,
            consume_pending_ask_action=consume_pending_ask_action,
        )

    async def run_discord_command_followup(
        self,
        *,
        tenant_id: str,
        user_id: str,
        channel_id: str,
        command_text: str,
        application_id: str,
        interaction_token: str,
        reply_to_message_id: str | None = None,
        command_params: dict[str, str] | None = None,
        attachments: list[dict[str, str]] | None = None,
    ) -> None:
        await _run_discord_command_followup(
            deps=self._deps,
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

    async def run_discord_ask_confirmation_followup(
        self,
        *,
        tenant_id: str,
        user_id: str,
        channel_id: str,
        decision: str,
        request_id: str,
        application_id: str,
        interaction_token: str,
    ) -> None:
        await _run_discord_ask_confirmation_followup(
            deps=self._deps,
            tenant_id=tenant_id,
            user_id=user_id,
            channel_id=channel_id,
            decision=decision,
            request_id=request_id,
            application_id=application_id,
            interaction_token=interaction_token,
        )
