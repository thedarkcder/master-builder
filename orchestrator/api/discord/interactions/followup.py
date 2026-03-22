from __future__ import annotations

import logging
import re

from sqlalchemy.orm import Session

from orchestrator.api.commands.entrypoint import execute_tenant_discord_ingress_command
from orchestrator.api.discord.ask.context import consume_pending_ask_action
from orchestrator.api.discord.interactions.followup_command_runtime import (
    run_discord_application_command_followup as _run_discord_application_command_followup_impl,
    run_discord_ask_confirmation_followup as _run_discord_ask_confirmation_followup_impl,
    run_discord_ask_confirmation_followup_blocking as _run_discord_ask_confirmation_followup_blocking_impl,
    run_discord_command_followup as _run_discord_command_followup_impl,
    run_discord_command_followup_blocking as _run_discord_command_followup_blocking_impl,
    run_discord_decision_gate_reply_followup as _run_discord_decision_gate_reply_followup_impl,
    run_discord_decision_gate_reply_followup_blocking as _run_discord_decision_gate_reply_followup_blocking_impl,
)
from orchestrator.api.discord.interactions.followup_runtime import (
    build_followup_service,
    resolve_tenant_id_for_followup,
    run_async_blocking,
)
from orchestrator.api.discord.interactions.followup_state import (
    ask_thread_message_map_from_config as _ask_thread_message_map_from_config_impl,
    decision_gate_issue_for_thread as _decision_gate_issue_for_thread_impl,
    project_ask_thread_channel_ids_for_tenant as _project_ask_thread_channel_ids_for_tenant_impl,
    project_channel_ids_for_tenant as _project_channel_ids_for_tenant_impl,
    project_seed_followup_thread_channel_ids_for_tenant as _project_seed_followup_thread_channel_ids_for_tenant_impl,
    resolve_thread_channel_for_reply as _resolve_thread_channel_for_reply_impl,
    resolve_project_for_channel as _resolve_project_for_channel_impl,
    resolve_thread_id_by_message_suffix as _resolve_thread_id_by_message_suffix_impl,
    tenant_discord_channel_ids as _tenant_discord_channel_ids_impl,
)
from orchestrator.api.discord.interactions.followup_threading import (
    send_discord_ask_response_with_thread as _send_discord_ask_response_with_thread_impl,
    send_discord_seed_followup_with_thread as _send_discord_seed_followup_with_thread_impl,
    send_discord_thread_followup as _send_discord_thread_followup_impl,
)
from orchestrator.api.discord.interactions.followup_transport import (
    discord_api_client as _discord_api_client_impl,
    send_discord_interaction_followup as _send_discord_interaction_followup_impl,
)
from orchestrator.api.discord.interactions.parser import _parse_discord_interaction_command
from orchestrator.api.discord.shared.followup_format import (
    build_ask_confirmation_components,
    build_command_followup_message,
    resolve_tenant_jira_browse_base_url,
)
from orchestrator.api.schemas import DiscordCommandRequest
from orchestrator.core.config import get_settings
from orchestrator.core.discord.channel_tenant_index import resolve_tenant_for_discord_channel
from orchestrator.core.discord.thread_context import put_thread_issue_key
from orchestrator.core.platform_secret_service import resolve_platform_secret_ref
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import Project, Tenant
from orchestrator.tools.discord_api import DiscordApiClient

logger = logging.getLogger(__name__)
ISSUE_KEY_PATTERN = re.compile(r"\b[A-Z][A-Z0-9_]+-\d+\b")

execute_discord_ingress_command = execute_tenant_discord_ingress_command


def _discord_api_client(*, session: Session, settings) -> DiscordApiClient:  # noqa: ANN001
    return _discord_api_client_impl(
        session=session,
        settings=settings,
        resolve_platform_secret_ref_fn=resolve_platform_secret_ref,
    )



def _tenant_discord_channel_ids(*, tenant: Tenant, project_channel_ids: set[str]) -> set[str]:
    return _tenant_discord_channel_ids_impl(tenant=tenant, project_channel_ids=project_channel_ids)



def _project_channel_ids_for_tenant(*, session: Session, tenant_id: str) -> set[str]:
    return _project_channel_ids_for_tenant_impl(session=session, tenant_id=tenant_id)


def _resolve_project_for_channel(
    *,
    session: Session,
    tenant: Tenant,
    channel_id: str,
) -> Project | None:
    return _resolve_project_for_channel_impl(
        session=session,
        tenant=tenant,
        channel_id=channel_id,
    )


def _project_ask_thread_channel_ids_for_tenant(*, session: Session, tenant_id: str) -> set[str]:
    return _project_ask_thread_channel_ids_for_tenant_impl(session=session, tenant_id=tenant_id)


def _project_seed_followup_thread_channel_ids_for_tenant(*, session: Session, tenant_id: str) -> set[str]:
    return _project_seed_followup_thread_channel_ids_for_tenant_impl(session=session, tenant_id=tenant_id)


def _ask_thread_message_map_from_config(discord_config: dict) -> dict[str, str]:
    return _ask_thread_message_map_from_config_impl(discord_config)


def _resolve_thread_id_by_message_suffix(
    *,
    client: DiscordApiClient,
    thread_ids: list[str],
    message_id: str,
) -> str | None:
    return _resolve_thread_id_by_message_suffix_impl(
        client=client,
        thread_ids=thread_ids,
        message_id=message_id,
    )



def _ask_confirmation_components(request_id: str) -> list[dict]:
    return build_ask_confirmation_components(request_id)



def _ask_reply_components() -> list[dict]:
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



def _decision_gate_issue_for_thread(*, session: Session, channel_id: str) -> tuple[str, str] | None:
    return _decision_gate_issue_for_thread_impl(
        session=session,
        channel_id=channel_id,
        issue_key_pattern=ISSUE_KEY_PATTERN,
    )


def _resolve_thread_channel_for_reply(
    *,
    session: Session,
    channel_id: str,
    reply_to_message_id: str,
) -> str:
    return _resolve_thread_channel_for_reply_impl(
        session=session,
        channel_id=channel_id,
        reply_to_message_id=reply_to_message_id,
    )



def _build_command_followup_message(
    *,
    session: Session,
    tenant: Tenant,
    user_id: str,
    command_response,
) -> str:  # noqa: ANN001
    return build_command_followup_message(
        user_id=user_id,
        command_response=command_response,
        jira_browse_base_url=resolve_tenant_jira_browse_base_url(session=session, tenant=tenant),
        issue_key_pattern=ISSUE_KEY_PATTERN,
    )



def _send_discord_interaction_followup(
    *,
    application_id: str,
    interaction_token: str,
    content: str,
    ephemeral: bool = False,
    components: list[dict] | None = None,
    reply_to_message_id: str | None = None,
    channel_id: str | None = None,
) -> None:
    return _send_discord_interaction_followup_impl(
        application_id=application_id,
        interaction_token=interaction_token,
        content=content,
        ephemeral=ephemeral,
        components=components,
        reply_to_message_id=reply_to_message_id,
        channel_id=channel_id,
    )


def _send_discord_thread_followup(
    *,
    session: Session,
    settings,
    tenant: Tenant,
    channel_id: str,
    reply_to_message_id: str,
    content: str,
    components: list[dict] | None = None,
    discord_api_client_fn=None,
    project_ask_thread_channel_ids_for_tenant_fn=None,
    project_seed_followup_thread_channel_ids_for_tenant_fn=None,
    resolve_project_for_channel_fn=None,
    ask_thread_message_map_from_config_fn=None,
    resolve_thread_id_by_message_suffix_fn=None,
) -> None:  # noqa: ANN001
    return _send_discord_thread_followup_impl(
        session=session,
        settings=settings,
        tenant=tenant,
        channel_id=channel_id,
        reply_to_message_id=reply_to_message_id,
        content=content,
        components=components,
        discord_api_client_fn=discord_api_client_fn or _discord_api_client,
        project_ask_thread_channel_ids_for_tenant_fn=(
            project_ask_thread_channel_ids_for_tenant_fn or _project_ask_thread_channel_ids_for_tenant
        ),
        project_seed_followup_thread_channel_ids_for_tenant_fn=(
            project_seed_followup_thread_channel_ids_for_tenant_fn or _project_seed_followup_thread_channel_ids_for_tenant
        ),
        resolve_project_for_channel_fn=resolve_project_for_channel_fn or _resolve_project_for_channel,
        ask_thread_message_map_from_config_fn=(
            ask_thread_message_map_from_config_fn or _ask_thread_message_map_from_config
        ),
        resolve_thread_id_by_message_suffix_fn=(
            resolve_thread_id_by_message_suffix_fn or _resolve_thread_id_by_message_suffix
        ),
    )


def _send_discord_ask_response_with_thread(
    *,
    session: Session,
    settings,
    tenant: Tenant,
    channel_id: str,
    user_id: str,
    content: str,
    components: list[dict] | None = None,
    issue_key: str | None = None,
    discord_api_client_fn=None,
    project_ask_thread_channel_ids_for_tenant_fn=None,
    resolve_project_for_channel_fn=None,
    ask_thread_message_map_from_config_fn=None,
    ask_reply_components_fn=None,
    put_thread_issue_key_fn=None,
) -> None:  # noqa: ANN001
    return _send_discord_ask_response_with_thread_impl(
        session=session,
        settings=settings,
        tenant=tenant,
        channel_id=channel_id,
        user_id=user_id,
        content=content,
        components=components,
        issue_key=issue_key,
        discord_api_client_fn=discord_api_client_fn or _discord_api_client,
        project_ask_thread_channel_ids_for_tenant_fn=(
            project_ask_thread_channel_ids_for_tenant_fn or _project_ask_thread_channel_ids_for_tenant
        ),
        resolve_project_for_channel_fn=resolve_project_for_channel_fn or _resolve_project_for_channel,
        ask_thread_message_map_from_config_fn=(
            ask_thread_message_map_from_config_fn or _ask_thread_message_map_from_config
        ),
        ask_reply_components_fn=ask_reply_components_fn or _ask_reply_components,
        put_thread_issue_key_fn=put_thread_issue_key_fn or put_thread_issue_key,
    )


def _send_discord_seed_followup_with_thread(
    *,
    session: Session,
    settings,
    tenant: Tenant,
    channel_id: str,
    user_id: str,
    content: str,
    request_id: str,
    questions: list[str],
    discord_api_client_fn=None,
    project_seed_followup_thread_channel_ids_for_tenant_fn=None,
    resolve_project_for_channel_fn=None,
) -> None:  # noqa: ANN001
    return _send_discord_seed_followup_with_thread_impl(
        session=session,
        settings=settings,
        tenant=tenant,
        channel_id=channel_id,
        user_id=user_id,
        content=content,
        request_id=request_id,
        questions=questions,
        discord_api_client_fn=discord_api_client_fn or _discord_api_client,
        project_seed_followup_thread_channel_ids_for_tenant_fn=(
            project_seed_followup_thread_channel_ids_for_tenant_fn
            or _project_seed_followup_thread_channel_ids_for_tenant
        ),
        resolve_project_for_channel_fn=resolve_project_for_channel_fn or _resolve_project_for_channel,
    )


async def _run_discord_command_followup(
    *,
    tenant_id: str | None,
    user_id: str,
    channel_id: str,
    command_text: str,
    application_id: str,
    interaction_token: str,
    reply_to_message_id: str | None = None,
    command_params: dict[str, str] | None = None,
    attachments: list[dict[str, str]] | None = None,
) -> None:
    await _run_discord_command_followup_impl(
        tenant_id=tenant_id,
        user_id=user_id,
        channel_id=channel_id,
        command_text=command_text,
        application_id=application_id,
        interaction_token=interaction_token,
        run_blocking_followup_fn=_run_discord_command_followup_blocking,
        reply_to_message_id=reply_to_message_id,
        command_params=command_params,
        attachments=attachments,
    )


async def _run_discord_application_command_followup(
    *,
    payload: dict,
    request_id: str,
) -> None:
    await _run_discord_application_command_followup_impl(
        payload=payload,
        request_id=request_id,
        parse_discord_interaction_command_fn=_parse_discord_interaction_command,
        send_interaction_followup_fn=_send_discord_interaction_followup,
        run_discord_command_followup_fn=_run_discord_command_followup,
    )


async def _run_discord_decision_gate_reply_followup(
    *,
    tenant_id: str | None,
    user_id: str,
    channel_id: str,
    issue_key: str,
    reply_text: str,
    application_id: str,
    interaction_token: str,
    reply_to_message_id: str | None = None,
) -> None:
    await _run_discord_decision_gate_reply_followup_impl(
        tenant_id=tenant_id,
        user_id=user_id,
        channel_id=channel_id,
        issue_key=issue_key,
        reply_text=reply_text,
        application_id=application_id,
        interaction_token=interaction_token,
        run_discord_command_followup_fn=_run_discord_command_followup,
        reply_to_message_id=reply_to_message_id,
    )



def _run_discord_decision_gate_reply_followup_blocking(
    *,
    tenant_id: str | None,
    user_id: str,
    channel_id: str,
    issue_key: str,
    reply_text: str,
    application_id: str,
    interaction_token: str,
    reply_to_message_id: str | None = None,
) -> None:
    return _run_discord_decision_gate_reply_followup_blocking_impl(
        tenant_id=tenant_id,
        user_id=user_id,
        channel_id=channel_id,
        issue_key=issue_key,
        reply_text=reply_text,
        application_id=application_id,
        interaction_token=interaction_token,
        run_discord_command_followup_blocking_fn=_run_discord_command_followup_blocking,
        reply_to_message_id=reply_to_message_id,
    )



def _run_discord_command_followup_blocking(
    *,
    tenant_id: str | None,
    user_id: str,
    channel_id: str,
    command_text: str,
    application_id: str,
    interaction_token: str,
    reply_to_message_id: str | None = None,
    command_params: dict[str, str] | None = None,
    attachments: list[dict[str, str]] | None = None,
) -> None:
    return _run_discord_command_followup_blocking_impl(
        tenant_id=tenant_id,
        user_id=user_id,
        channel_id=channel_id,
        command_text=command_text,
        application_id=application_id,
        interaction_token=interaction_token,
        create_session_factory_fn=create_session_factory,
        resolve_tenant_id_for_followup_fn=resolve_tenant_id_for_followup,
        resolve_tenant_for_channel_fn=resolve_tenant_for_discord_channel,
        build_followup_service_fn=build_followup_service,
        run_async_blocking_fn=run_async_blocking,
        settings_factory_fn=get_settings,
        execute_command_ingress=execute_discord_ingress_command,
        command_request_factory=DiscordCommandRequest,
        build_command_followup_message_fn=_build_command_followup_message,
        ask_confirmation_components_fn=_ask_confirmation_components,
        ask_reply_components_fn=_ask_reply_components,
        send_interaction_followup_fn=_send_discord_interaction_followup,
        send_thread_reply_fn=_send_discord_thread_followup,
        send_ask_with_thread_fn=_send_discord_ask_response_with_thread,
        send_seed_with_thread_fn=_send_discord_seed_followup_with_thread,
        consume_pending_ask_action_fn=consume_pending_ask_action,
        reply_to_message_id=reply_to_message_id,
        command_params=command_params,
        attachments=attachments,
    )


async def _run_discord_ask_confirmation_followup(
    *,
    tenant_id: str | None,
    user_id: str,
    channel_id: str,
    decision: str,
    request_id: str,
    application_id: str,
    interaction_token: str,
) -> None:
    await _run_discord_ask_confirmation_followup_impl(
        tenant_id=tenant_id,
        user_id=user_id,
        channel_id=channel_id,
        decision=decision,
        request_id=request_id,
        application_id=application_id,
        interaction_token=interaction_token,
        run_discord_ask_confirmation_followup_blocking_fn=_run_discord_ask_confirmation_followup_blocking,
    )



def _run_discord_ask_confirmation_followup_blocking(
    *,
    tenant_id: str | None,
    user_id: str,
    channel_id: str,
    decision: str,
    request_id: str,
    application_id: str,
    interaction_token: str,
) -> None:
    return _run_discord_ask_confirmation_followup_blocking_impl(
        tenant_id=tenant_id,
        user_id=user_id,
        channel_id=channel_id,
        decision=decision,
        request_id=request_id,
        application_id=application_id,
        interaction_token=interaction_token,
        create_session_factory_fn=create_session_factory,
        resolve_tenant_id_for_followup_fn=resolve_tenant_id_for_followup,
        resolve_tenant_for_channel_fn=resolve_tenant_for_discord_channel,
        build_followup_service_fn=build_followup_service,
        run_async_blocking_fn=run_async_blocking,
        settings_factory_fn=get_settings,
        execute_command_ingress=execute_discord_ingress_command,
        command_request_factory=DiscordCommandRequest,
        build_command_followup_message_fn=_build_command_followup_message,
        ask_confirmation_components_fn=_ask_confirmation_components,
        ask_reply_components_fn=_ask_reply_components,
        send_interaction_followup_fn=_send_discord_interaction_followup,
        send_thread_reply_fn=_send_discord_thread_followup,
        send_ask_with_thread_fn=_send_discord_ask_response_with_thread,
        send_seed_with_thread_fn=_send_discord_seed_followup_with_thread,
        consume_pending_ask_action_fn=consume_pending_ask_action,
    )
