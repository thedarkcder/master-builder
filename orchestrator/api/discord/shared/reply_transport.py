from __future__ import annotations

from collections.abc import Callable

from orchestrator.core.communications import (
    DiscordInteractionFollowupAction,
)
from orchestrator.core.discord.transport_executor import DiscordTransportExecutor


class DiscordReplyTransport:
    def __init__(
        self,
        *,
        send_interaction_followup: Callable[..., None],
        send_thread_reply: Callable[..., None],
        send_ask_with_thread: Callable[..., None],
        send_seed_with_thread: Callable[..., None],
    ) -> None:
        self._executor = DiscordTransportExecutor(interaction_followup_sender=send_interaction_followup)
        self._send_thread_reply = send_thread_reply
        self._send_ask_with_thread = send_ask_with_thread
        self._send_seed_with_thread = send_seed_with_thread

    def send_interaction_followup(
        self,
        *,
        application_id: str,
        interaction_token: str,
        content: str,
        ephemeral: bool = False,
        components: list[dict] | None = None,
        reply_to_message_id: str | None = None,
        channel_id: str | None = None,
    ) -> None:
        self._executor.execute(
            action=DiscordInteractionFollowupAction(
                application_id=application_id,
                interaction_token=interaction_token,
                content=content,
                ephemeral=ephemeral,
                components=components,
                reply_to_message_id=reply_to_message_id,
                channel_id=channel_id,
            )
        )

    def send_thread_reply(
        self,
        *,
        session,
        settings,
        tenant,
        channel_id: str,
        reply_to_message_id: str,
        content: str,
        components: list[dict] | None = None,
    ) -> None:
        self._send_thread_reply(
            session=session,
            settings=settings,
            tenant=tenant,
            channel_id=channel_id,
            reply_to_message_id=reply_to_message_id,
            content=content,
            components=components,
        )

    def send_ask_with_thread(
        self,
        *,
        session,
        settings,
        tenant,
        channel_id: str,
        user_id: str,
        content: str,
        components: list[dict] | None = None,
        issue_key: str | None = None,
    ) -> None:
        self._send_ask_with_thread(
            session=session,
            settings=settings,
            tenant=tenant,
            channel_id=channel_id,
            user_id=user_id,
            content=content,
            components=components,
            issue_key=issue_key,
        )

    def send_seed_with_thread(
        self,
        *,
        session,
        settings,
        tenant,
        channel_id: str,
        user_id: str,
        content: str,
        request_id: str,
        questions: list[str],
    ) -> None:
        self._send_seed_with_thread(
            session=session,
            settings=settings,
            tenant=tenant,
            channel_id=channel_id,
            user_id=user_id,
            content=content,
            request_id=request_id,
            questions=questions,
        )
