from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any

from sqlalchemy.orm import Session

from orchestrator.api.discord.interactions.followup_state import (
    ask_thread_message_map_from_config,
    project_ask_thread_channel_ids_for_tenant,
    project_seed_followup_thread_channel_ids_for_tenant,
    resolve_project_for_channel,
    resolve_thread_id_by_message_suffix,
)
from orchestrator.api.discord.interactions.followup_threading import (
    send_discord_ask_response_with_thread,
    send_discord_seed_followup_with_thread,
    send_discord_thread_followup,
)
from orchestrator.api.discord.interactions.followup_transport import (
    discord_api_client,
    send_discord_interaction_callback,
)
from orchestrator.core.communications import (
    DiscordAskWithThreadAction,
    DiscordChannelMessageAction,
    DiscordChannelMessageWithAttachmentAction,
    DiscordInteractionFollowupAction,
    DiscordInteractionResponseAction,
    DiscordSeedWithThreadAction,
    DiscordTenantNotificationAction,
    DiscordThreadReplyAction,
    TransportAction,
)
from orchestrator.core.discord.notifications import send_tenant_discord_message
from orchestrator.core.platform.secret_service import resolve_platform_secret_ref
from orchestrator.storage.models import Project, Tenant
from orchestrator.tools.discord_api import DiscordApiClient


class DiscordThreadActionHandler:
    def __init__(
        self,
        *,
        session_factory: Callable[[], AbstractContextManager[Session]],
        settings_factory: Callable[[], object],
        resolve_platform_secret_ref_fn=resolve_platform_secret_ref,
        discord_api_client_fn=discord_api_client,
        thread_followup_sender=send_discord_thread_followup,
        ask_with_thread_sender=send_discord_ask_response_with_thread,
        seed_with_thread_sender=send_discord_seed_followup_with_thread,
    ) -> None:
        self._session_factory = session_factory
        self._settings_factory = settings_factory
        self._resolve_platform_secret_ref_fn = resolve_platform_secret_ref_fn
        self._discord_api_client_fn = discord_api_client_fn
        self._thread_followup_sender = thread_followup_sender
        self._ask_with_thread_sender = ask_with_thread_sender
        self._seed_with_thread_sender = seed_with_thread_sender

    def execute_thread_reply(self, *, action: DiscordThreadReplyAction) -> None:
        self._with_followup_context(
            tenant_id=action.tenant_id,
            callback=lambda session, settings, tenant: self._thread_followup_sender(
                session=session,
                settings=settings,
                tenant=tenant,
                channel_id=action.channel_id,
                reply_to_message_id=action.reply_to_message_id,
                content=action.content,
                components=action.components,
                discord_api_client_fn=self._discord_api_client,
                project_ask_thread_channel_ids_for_tenant_fn=project_ask_thread_channel_ids_for_tenant,
                project_seed_followup_thread_channel_ids_for_tenant_fn=project_seed_followup_thread_channel_ids_for_tenant,
                resolve_project_for_channel_fn=resolve_project_for_channel,
                ask_thread_message_map_from_config_fn=ask_thread_message_map_from_config,
                resolve_thread_id_by_message_suffix_fn=resolve_thread_id_by_message_suffix,
            ),
        )

    def execute_ask_with_thread(self, *, action: DiscordAskWithThreadAction) -> None:
        self._with_followup_context(
            tenant_id=action.tenant_id,
            callback=lambda session, settings, tenant: self._ask_with_thread_sender(
                session=session,
                settings=settings,
                tenant=tenant,
                channel_id=action.channel_id,
                user_id=action.user_id,
                content=action.content,
                components=action.components,
                issue_key=action.issue_key,
                followup_context_type=action.followup_context_type,
                request_id=action.request_id,
                discord_api_client_fn=self._discord_api_client,
                project_ask_thread_channel_ids_for_tenant_fn=project_ask_thread_channel_ids_for_tenant,
                resolve_project_for_channel_fn=resolve_project_for_channel,
                ask_thread_message_map_from_config_fn=ask_thread_message_map_from_config,
                ask_reply_components_fn=_default_ask_reply_components,
            ),
        )

    def execute_seed_with_thread(self, *, action: DiscordSeedWithThreadAction) -> None:
        self._with_followup_context(
            tenant_id=action.tenant_id,
            callback=lambda session, settings, tenant: self._seed_with_thread_sender(
                session=session,
                settings=settings,
                tenant=tenant,
                channel_id=action.channel_id,
                user_id=action.user_id,
                content=action.content,
                request_id=action.request_id,
                questions=action.questions,
                discord_api_client_fn=self._discord_api_client,
                project_seed_followup_thread_channel_ids_for_tenant_fn=project_seed_followup_thread_channel_ids_for_tenant,
                resolve_project_for_channel_fn=resolve_project_for_channel,
            ),
        )

    def _with_followup_context(
        self,
        *,
        tenant_id: str,
        callback: Callable[[Session, object, Tenant], None],
    ) -> None:
        normalized_tenant_id = str(tenant_id or "").strip()
        if not normalized_tenant_id:
            raise RuntimeError("Discord follow-up action is missing tenant context")
        settings = self._settings_factory()
        with self._session_factory() as session:
            tenant = session.get(Tenant, normalized_tenant_id)
            if tenant is None or not tenant.is_enabled:
                raise RuntimeError(
                    f"Discord tenant '{normalized_tenant_id}' is unavailable for follow-up action"
                )
            callback(session, settings, tenant)

    def _discord_api_client(self, *, session: Session, settings) -> DiscordApiClient:  # noqa: ANN001
        return self._discord_api_client_fn(
            session=session,
            settings=settings,
            resolve_platform_secret_ref_fn=self._resolve_platform_secret_ref_fn,
        )


class DiscordNotificationActionHandler:
    def __init__(
        self,
        *,
        session_factory: Callable[[], AbstractContextManager[Session]],
        settings_factory: Callable[[], object],
        send_tenant_discord_message_fn=None,
    ) -> None:
        self._session_factory = session_factory
        self._settings_factory = settings_factory
        self._send_tenant_discord_message_fn = (
            send_tenant_discord_message_fn or send_tenant_discord_message
        )

    def execute_tenant_notification(
        self, *, action: DiscordTenantNotificationAction
    ) -> None:
        normalized_tenant_id = str(action.tenant_id or "").strip()
        if not normalized_tenant_id:
            raise RuntimeError(
                "Discord tenant notification action is missing tenant context"
            )
        settings = self._settings_factory()
        with self._session_factory() as session:
            tenant = session.get(Tenant, normalized_tenant_id)
            if tenant is None or not tenant.is_enabled:
                raise RuntimeError(
                    f"Discord tenant '{normalized_tenant_id}' is unavailable for notification action"
                )
            project = None
            normalized_project_id = str(action.project_id or "").strip()
            if normalized_project_id:
                project = session.get(Project, normalized_project_id)
            self._send_tenant_discord_message_fn(
                session=session,
                tenant=tenant,
                project=project,
                message=action.message,
                settings=settings,
                event=action.event,
                open_thread=action.open_thread,
                thread_name=action.thread_name,
                thread_intro=action.thread_intro,
                thread_intro_components=action.thread_intro_components,
            )


class DiscordTransportExecutor:
    def __init__(
        self,
        *,
        bot_token: str | None = None,
        interaction_callback_sender=send_discord_interaction_callback,
        interaction_followup_sender=None,
        thread_action_handler: DiscordThreadActionHandler | None = None,
        notification_action_handler: DiscordNotificationActionHandler | None = None,
        client_factory=DiscordApiClient,
        session_factory: Callable[[], AbstractContextManager[Session]] | None = None,
        settings_factory: Callable[[], object] | None = None,
        resolve_platform_secret_ref_fn=resolve_platform_secret_ref,
        thread_followup_sender=send_discord_thread_followup,
        ask_with_thread_sender=send_discord_ask_response_with_thread,
        seed_with_thread_sender=send_discord_seed_followup_with_thread,
    ) -> None:
        self._bot_token = str(bot_token or "").strip() or None
        self._interaction_callback_sender = interaction_callback_sender
        self._interaction_followup_sender = interaction_followup_sender
        self._client_factory = client_factory
        self._thread_action_handler = thread_action_handler
        self._notification_action_handler = notification_action_handler
        if (
            self._thread_action_handler is None
            and session_factory is not None
            and settings_factory is not None
        ):
            self._thread_action_handler = DiscordThreadActionHandler(
                session_factory=session_factory,
                settings_factory=settings_factory,
                resolve_platform_secret_ref_fn=resolve_platform_secret_ref_fn,
                thread_followup_sender=thread_followup_sender,
                ask_with_thread_sender=ask_with_thread_sender,
                seed_with_thread_sender=seed_with_thread_sender,
            )
        if (
            self._notification_action_handler is None
            and session_factory is not None
            and settings_factory is not None
        ):
            self._notification_action_handler = DiscordNotificationActionHandler(
                session_factory=session_factory,
                settings_factory=settings_factory,
            )

    def execute(self, *, action: TransportAction) -> dict[str, Any] | None:
        if isinstance(action, DiscordInteractionResponseAction):
            self._interaction_callback_sender(
                interaction_id=action.interaction_id,
                interaction_token=action.interaction_token,
                response_body=(
                    action.body.encode("utf-8")
                    if isinstance(action.body, str)
                    else action.body
                ),
            )
            return
        if isinstance(action, DiscordChannelMessageAction):
            data = self._require_client().post_message(
                channel_id=action.channel_id,
                content=action.content,
                components=action.components,
            )
            return self._message_result(data=data, channel_id=action.channel_id)
        if isinstance(action, DiscordChannelMessageWithAttachmentAction):
            return self._execute_attachment_action(action=action)
        if isinstance(action, DiscordInteractionFollowupAction):
            if self._interaction_followup_sender is None:
                raise RuntimeError(
                    "Discord interaction follow-up sender is not configured"
                )
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
            self._require_thread_action_handler().execute_thread_reply(action=action)
            return
        if isinstance(action, DiscordAskWithThreadAction):
            self._require_thread_action_handler().execute_ask_with_thread(action=action)
            return
        if isinstance(action, DiscordSeedWithThreadAction):
            self._require_thread_action_handler().execute_seed_with_thread(
                action=action
            )
            return
        if isinstance(action, DiscordTenantNotificationAction):
            self._require_notification_action_handler().execute_tenant_notification(
                action=action
            )
            return
        raise RuntimeError(
            f"Unsupported Discord transport action: {type(action).__name__}"
        )

    def _require_client(self) -> DiscordApiClient:
        if not self._bot_token:
            raise RuntimeError(
                "Discord bot token is not configured for message actions"
            )
        return self._client_factory(bot_token=self._bot_token)

    def _require_thread_action_handler(self) -> DiscordThreadActionHandler:
        if self._thread_action_handler is None:
            raise RuntimeError("Discord thread action handler is not configured")
        return self._thread_action_handler

    def _require_notification_action_handler(self) -> DiscordNotificationActionHandler:
        if self._notification_action_handler is None:
            raise RuntimeError("Discord notification action handler is not configured")
        return self._notification_action_handler

    def _execute_attachment_action(
        self, *, action: DiscordChannelMessageWithAttachmentAction
    ) -> dict[str, Any] | None:
        try:
            data = self._require_client().post_message_with_attachment(
                channel_id=action.channel_id,
                content=action.content,
                filename=action.filename,
                file_bytes=action.file_bytes,
                content_type=action.content_type,
                components=action.components,
            )
            return self._message_result(data=data, channel_id=action.channel_id)
        except Exception as exc:
            fallback_content = str(action.fallback_content_on_failure or "").strip()
            if not fallback_content and action.failure_user_id:
                fallback_content = f"<@{action.failure_user_id}> I couldn't send the voice reply attachment."
            if fallback_content:
                data = self._require_client().post_message(
                    channel_id=action.channel_id,
                    content=fallback_content,
                    components=action.fallback_components_on_failure,
                )
                return self._message_result(data=data, channel_id=action.channel_id)
            if action.failure_user_id:
                self._require_client().post_message(
                    channel_id=action.channel_id,
                    content=f"<@{action.failure_user_id}> Voice reply post failed: {exc}",
                )
            elif not fallback_content:
                raise
        return None

    def _message_result(
        self, *, data: dict[str, Any], channel_id: str
    ) -> dict[str, Any]:
        message_id = str(data.get("id") or "").strip()
        if not message_id:
            raise RuntimeError("Discord transport action did not return a message id")
        return {
            "message_id": message_id,
            "channel_id": str(data.get("channel_id") or "").strip() or channel_id,
        }


def _default_ask_reply_components() -> list[dict]:
    return [
        {
            "type": 1,
            "components": [
                {
                    "type": 2,
                    "style": 1,
                    "label": "Reply",
                    "custom_id": "ask.reply.open",
                }
            ],
        }
    ]
