from __future__ import annotations

from orchestrator.api.discord.interactions.followup_transport import send_discord_interaction_callback
from orchestrator.core.communications import (
    DiscordAskWithThreadAction,
    DiscordChannelMessageAction,
    DiscordChannelMessageWithAttachmentAction,
    DiscordInteractionFollowupAction,
    DiscordInteractionResponseAction,
    DiscordSeedWithThreadAction,
    DiscordThreadReplyAction,
    TransportAction,
)
from orchestrator.tools.discord_api import DiscordApiClient


class DiscordTransportExecutor:
    def __init__(
        self,
        *,
        bot_token: str | None = None,
        interaction_callback_sender=send_discord_interaction_callback,
        interaction_followup_sender=None,
        thread_reply_sender=None,
        ask_with_thread_sender=None,
        seed_with_thread_sender=None,
        client_factory=DiscordApiClient,
    ) -> None:
        self._bot_token = str(bot_token or "").strip() or None
        self._interaction_callback_sender = interaction_callback_sender
        self._interaction_followup_sender = interaction_followup_sender
        self._thread_reply_sender = thread_reply_sender
        self._ask_with_thread_sender = ask_with_thread_sender
        self._seed_with_thread_sender = seed_with_thread_sender
        self._client_factory = client_factory

    def execute(self, *, action: TransportAction) -> None:
        if isinstance(action, DiscordInteractionResponseAction):
            self._interaction_callback_sender(
                interaction_id=action.interaction_id,
                interaction_token=action.interaction_token,
                response_body=(action.body.encode("utf-8") if isinstance(action.body, str) else action.body),
            )
            return
        if isinstance(action, DiscordChannelMessageAction):
            self._require_client().post_message(
                channel_id=action.channel_id,
                content=action.content,
                components=action.components,
            )
            return
        if isinstance(action, DiscordChannelMessageWithAttachmentAction):
            self._execute_attachment_action(action=action)
            return
        if isinstance(action, DiscordInteractionFollowupAction):
            if self._interaction_followup_sender is None:
                raise RuntimeError("Discord interaction follow-up sender is not configured")
            self._interaction_followup_sender(
                application_id=action.application_id,
                interaction_token=action.interaction_token,
                content=action.content,
                ephemeral=action.ephemeral,
                components=action.components,
                reply_to_message_id=action.reply_to_message_id,
                channel_id=action.channel_id,
            )
            return
        if isinstance(action, DiscordThreadReplyAction):
            if self._thread_reply_sender is None:
                raise RuntimeError("Discord thread reply sender is not configured")
            self._thread_reply_sender(
                session=action.session,
                settings=action.settings,
                tenant=action.tenant,
                channel_id=action.channel_id,
                reply_to_message_id=action.reply_to_message_id,
                content=action.content,
                components=action.components,
            )
            return
        if isinstance(action, DiscordAskWithThreadAction):
            if self._ask_with_thread_sender is None:
                raise RuntimeError("Discord ask-with-thread sender is not configured")
            self._ask_with_thread_sender(
                session=action.session,
                settings=action.settings,
                tenant=action.tenant,
                channel_id=action.channel_id,
                user_id=action.user_id,
                content=action.content,
                components=action.components,
                issue_key=action.issue_key,
            )
            return
        if isinstance(action, DiscordSeedWithThreadAction):
            if self._seed_with_thread_sender is None:
                raise RuntimeError("Discord seed-with-thread sender is not configured")
            self._seed_with_thread_sender(
                session=action.session,
                settings=action.settings,
                tenant=action.tenant,
                channel_id=action.channel_id,
                user_id=action.user_id,
                content=action.content,
                request_id=action.request_id,
                questions=action.questions,
            )
            return
        raise RuntimeError(f"Unsupported Discord transport action: {type(action).__name__}")

    def _require_client(self):
        if not self._bot_token:
            raise RuntimeError("Discord bot token is not configured for message actions")
        return self._client_factory(bot_token=self._bot_token)

    def _execute_attachment_action(self, *, action: DiscordChannelMessageWithAttachmentAction) -> None:
        try:
            self._require_client().post_message_with_attachment(
                channel_id=action.channel_id,
                content=action.content,
                filename=action.filename,
                file_bytes=action.file_bytes,
                content_type=action.content_type,
                components=action.components,
            )
        except Exception as exc:
            fallback_content = str(action.fallback_content_on_failure or "").strip()
            if not fallback_content and action.failure_user_id:
                fallback_content = f"<@{action.failure_user_id}> I couldn't send the voice reply attachment."
            if fallback_content:
                self._require_client().post_message(
                    channel_id=action.channel_id,
                    content=fallback_content,
                    components=action.fallback_components_on_failure,
                )
            if action.failure_user_id:
                self._require_client().post_message(
                    channel_id=action.channel_id,
                    content=f"<@{action.failure_user_id}> Voice reply post failed: {exc}",
                )
            elif not fallback_content:
                raise
