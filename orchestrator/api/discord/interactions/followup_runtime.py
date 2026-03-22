from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractContextManager

from sqlalchemy.orm import Session

from orchestrator.api.webhooks.followup_service import DiscordWebhookFollowupService
from orchestrator.core.discord.transport_executor import DiscordTransportExecutor
from orchestrator.core.platform_secret_service import resolve_platform_secret_ref
from orchestrator.storage.models import Tenant


def resolve_tenant_id_for_followup(
    *,
    session_factory: Callable[[], AbstractContextManager[Session]],
    resolve_tenant_for_channel_fn: Callable[..., Tenant | None],
    tenant_id: str | None,
    channel_id: str,
) -> str:
    resolved_tenant_id = str(tenant_id or "").strip()
    if resolved_tenant_id:
        return resolved_tenant_id
    with session_factory() as session:
        tenant = resolve_tenant_for_channel_fn(session=session, channel_id=channel_id)
        return tenant.tenant_id if tenant is not None else ""


def build_followup_service(
    *,
    session_factory: Callable[[], AbstractContextManager[Session]],
    settings_factory: Callable[[], object],
    execute_command_ingress: Callable[..., object],
    command_request_factory: Callable[..., object],
    build_command_followup_message: Callable[..., str],
    ask_confirmation_components: Callable[[str], list[dict]],
    ask_reply_components: Callable[[], list[dict]],
    send_interaction_followup: Callable[..., None],
    send_thread_reply: Callable[..., None],
    send_ask_with_thread: Callable[..., None],
    send_seed_with_thread: Callable[..., None],
    consume_pending_ask_action: Callable[..., dict | None],
) -> DiscordWebhookFollowupService:
    return DiscordWebhookFollowupService(
        session_factory=session_factory,
        settings_factory=settings_factory,
        execute_command_ingress=execute_command_ingress,
        command_request_factory=command_request_factory,
        build_command_followup_message=build_command_followup_message,
        ask_confirmation_components=ask_confirmation_components,
        ask_reply_components=ask_reply_components,
        transport_executor=DiscordTransportExecutor(
            interaction_followup_sender=send_interaction_followup,
            session_factory=session_factory,
            settings_factory=settings_factory,
            resolve_platform_secret_ref_fn=resolve_platform_secret_ref,
            thread_followup_sender=send_thread_reply,
            ask_with_thread_sender=send_ask_with_thread,
            seed_with_thread_sender=send_seed_with_thread,
        ),
        consume_pending_ask_action=consume_pending_ask_action,
    )


def run_async_blocking(awaitable_factory: Callable[[], object]) -> None:
    asyncio.run(awaitable_factory())
