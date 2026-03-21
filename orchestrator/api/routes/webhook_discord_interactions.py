from __future__ import annotations

import asyncio
import logging
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from orchestrator.api.dependencies import get_session
from orchestrator.api.discord.interactions.application import build_discord_interaction_ingress_result
from orchestrator.api.discord.interactions.auth import (
    ASK_REPLY_OPEN_CUSTOM_ID,
    _discord_autocomplete_response,
    _discord_interaction_deferred_response,
    _discord_interaction_modal_response,
    _discord_interaction_response,
    _discord_modal_text_value,
    _parse_ask_confirmation_custom_id,
    _parse_ask_reply_modal_custom_id,
    _resolve_discord_interactions_public_key,
    _validate_discord_interaction_signature,
)
from orchestrator.api.discord.interactions.dispatcher import DiscordInteractionDispatchDeps
from orchestrator.api.discord.interactions.followup import (
    _decision_gate_issue_for_thread,
    _run_discord_application_command_followup,
    _run_discord_ask_confirmation_followup,
    _run_discord_command_followup,
    _run_discord_decision_gate_reply_followup,
)
from orchestrator.api.discord.interactions.parser import (
    _discord_issue_autocomplete_choices,
    _find_focused_discord_option,
    _find_tenant_for_discord_channel,
)
from orchestrator.api.webhooks.payload_utils import read_json_payload as _read_json_payload
from orchestrator.api.transport_runtime import execute_http_ingress_result
from orchestrator.core.communications import (
    DiscordInteractionResponseAction,
    HttpJsonResponseBytesAction,
    IngressResult,
    TransportAction,
    TransportEnvelope,
)
from orchestrator.core.config import get_settings

router = APIRouter(tags=["discord-interactions"])
logger = logging.getLogger(__name__)


def _dispatch_deps(*, task_scheduler) -> DiscordInteractionDispatchDeps:  # noqa: ANN001
    return DiscordInteractionDispatchDeps(
        ask_reply_open_custom_id=ASK_REPLY_OPEN_CUSTOM_ID,
        autocomplete_response=_discord_autocomplete_response,
        interaction_response=_discord_interaction_response,
        interaction_modal_response=_discord_interaction_modal_response,
        interaction_deferred_response=_discord_interaction_deferred_response,
        parse_ask_confirmation_custom_id=_parse_ask_confirmation_custom_id,
        parse_ask_reply_modal_custom_id=_parse_ask_reply_modal_custom_id,
        discord_modal_text_value=_discord_modal_text_value,
        find_tenant_for_discord_channel=_find_tenant_for_discord_channel,
        find_focused_discord_option=_find_focused_discord_option,
        discord_issue_autocomplete_choices=_discord_issue_autocomplete_choices,
        decision_gate_issue_for_thread=_decision_gate_issue_for_thread,
        run_discord_ask_confirmation_followup=_run_discord_ask_confirmation_followup,
        run_discord_command_followup=_run_discord_command_followup,
        run_discord_decision_gate_reply_followup=_run_discord_decision_gate_reply_followup,
        run_discord_application_command_followup=_run_discord_application_command_followup,
        task_scheduler=task_scheduler,
        logger=logger,
    )


def _http_result_from_interaction_result(result: IngressResult) -> IngressResult:
    http_actions: list[TransportAction] = []
    for action in result.actions:
        if not isinstance(action, DiscordInteractionResponseAction):
            http_actions.append(action)
            continue
        http_actions.append(
            HttpJsonResponseBytesAction(
                status_code=int(action.status_code),
                body=action.body,
                headers={"content-type": "application/json"},
            )
        )
    return IngressResult(actions=tuple(http_actions), deferred_work=result.deferred_work)


@router.post("/discord/interactions")
async def ingest_discord_interaction(
    request: Request,
    session: Session = Depends(get_session),
) -> object:
    settings = get_settings()
    request_id = request.headers.get("X-Request-Id") or str(uuid4())

    payload, payload_bytes = await _read_json_payload(request, request_id=request_id, source="discord")
    public_key = _resolve_discord_interactions_public_key(session=session, settings=settings)
    _validate_discord_interaction_signature(
        request=request,
        payload_bytes=payload_bytes,
        public_key=public_key,
    )

    result = await build_discord_interaction_ingress_result(
        payload=payload,
        session=session,
        envelope=TransportEnvelope(
            transport="discord_http",
            event_type="interaction_create",
            request_id=request_id,
            payload=payload,
        ),
        dispatch_deps=_dispatch_deps(task_scheduler=asyncio.create_task),
    )
    return execute_http_ingress_result(
        result=_http_result_from_interaction_result(result),
        task_scheduler=asyncio.create_task,
    )
