from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import replace

from fastapi import HTTPException
from sqlalchemy.orm import Session

from orchestrator.api.discord.interactions.auth import (
    ASK_REPLY_OPEN_CUSTOM_ID,
    _discord_autocomplete_response,
    _discord_interaction_deferred_response,
    _discord_interaction_modal_response,
    _discord_interaction_response,
    _discord_modal_text_value,
    _parse_ask_confirmation_custom_id,
    _parse_install_request_decision_custom_id,
    _parse_ask_reply_modal_custom_id,
)
from orchestrator.api.discord.interactions.dispatcher import (
    DiscordInteractionDispatchDeps,
    dispatch_discord_interaction,
)
from orchestrator.api.discord.interactions.followup import (
    _resolve_followup_context_match,
    _resolve_followup_context,
    _resolve_followup_reaction,
    _resolve_thread_channel_for_reply,
    _run_discord_application_command_followup,
    _run_discord_ask_confirmation_followup,
    _run_discord_command_followup,
    _run_discord_decision_gate_reply_followup,
    _run_project_install_request_decision_followup,
)
from orchestrator.api.discord.interactions.parser import (
    _discord_issue_autocomplete_choices,
    _find_focused_discord_option,
    _find_tenant_for_discord_channel,
)
from orchestrator.core.communications import (
    DeferredTransportWork,
    DiscordInteractionResponseAction,
    IngressResult,
    TransportEnvelope,
)


async def build_discord_interaction_ingress_result(
    *,
    payload: dict,
    session: Session,
    envelope: TransportEnvelope,
    dispatch_deps: DiscordInteractionDispatchDeps,
) -> IngressResult:
    deferred: list[DeferredTransportWork] = []

    def _capture_deferred(coro: Awaitable[None]) -> DeferredTransportWork:
        work = DeferredTransportWork(
            kind="discord_interaction_deferred",
            runner=lambda coro=coro: coro,
            metadata={
                "request_id": envelope.request_id,
                "transport": envelope.transport,
                "event_type": envelope.event_type,
            },
        )
        deferred.append(work)
        return work

    response = await dispatch_discord_interaction(
        payload=payload,
        session=session,
        request_id=envelope.request_id,
        deps=replace(dispatch_deps, task_scheduler=_capture_deferred),
    )

    return IngressResult(
        actions=(
            DiscordInteractionResponseAction(
                interaction_id=str(payload.get("id") or "").strip(),
                interaction_token=str(payload.get("token") or "").strip(),
                status_code=response.status_code,
                body=response.body,
            ),
        ),
        deferred_work=tuple(deferred),
    )


def build_default_discord_interaction_dispatch_deps(*, task_scheduler, logger) -> DiscordInteractionDispatchDeps:  # noqa: ANN001
    return DiscordInteractionDispatchDeps(
        transport_source="discord_http",
        ask_reply_open_custom_id=ASK_REPLY_OPEN_CUSTOM_ID,
        autocomplete_response=_discord_autocomplete_response,
        interaction_response=_discord_interaction_response,
        interaction_modal_response=_discord_interaction_modal_response,
        interaction_deferred_response=_discord_interaction_deferred_response,
        parse_ask_confirmation_custom_id=_parse_ask_confirmation_custom_id,
        parse_install_request_decision_custom_id=_parse_install_request_decision_custom_id,
        parse_ask_reply_modal_custom_id=_parse_ask_reply_modal_custom_id,
        discord_modal_text_value=_discord_modal_text_value,
        find_tenant_for_discord_channel=_find_tenant_for_discord_channel,
        find_focused_discord_option=_find_focused_discord_option,
        discord_issue_autocomplete_choices=_discord_issue_autocomplete_choices,
        resolve_thread_channel_for_reply=_resolve_thread_channel_for_reply,
        resolve_followup_context_match=_resolve_followup_context_match,
        resolve_followup_context=_resolve_followup_context,
        resolve_followup_reaction=_resolve_followup_reaction,
        run_discord_ask_confirmation_followup=_run_discord_ask_confirmation_followup,
        run_project_install_request_decision_followup=_run_project_install_request_decision_followup,
        run_discord_command_followup=_run_discord_command_followup,
        run_discord_decision_gate_reply_followup=_run_discord_decision_gate_reply_followup,
        run_discord_application_command_followup=_run_discord_application_command_followup,
        task_scheduler=task_scheduler,
        logger=logger,
    )


def require_discord_interaction_context(action: DiscordInteractionResponseAction) -> tuple[str, str]:
    interaction_id = str(action.interaction_id or "").strip()
    interaction_token = str(action.interaction_token or "").strip()
    if not interaction_id or not interaction_token:
        raise HTTPException(status_code=400, detail="Missing Discord interaction context")
    return interaction_id, interaction_token
