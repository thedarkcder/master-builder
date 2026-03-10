from __future__ import annotations

import asyncio
import logging
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from orchestrator.api.dependencies import get_session
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
from orchestrator.api.discord.interactions.dispatcher import (
    DiscordInteractionDispatchDeps,
    dispatch_discord_interaction,
)
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
from orchestrator.core.config import get_settings

router = APIRouter(tags=["discord-interactions"])
logger = logging.getLogger(__name__)


@router.post("/discord/interactions")
async def ingest_discord_interaction(
    request: Request,
    session: Session = Depends(get_session),
) -> JSONResponse:
    settings = get_settings()
    request_id = request.headers.get("X-Request-Id") or str(uuid4())

    payload, payload_bytes = await _read_json_payload(request, request_id=request_id, source="discord")
    public_key = _resolve_discord_interactions_public_key(session=session, settings=settings)
    _validate_discord_interaction_signature(
        request=request,
        payload_bytes=payload_bytes,
        public_key=public_key,
    )

    return await dispatch_discord_interaction(
        payload=payload,
        session=session,
        request_id=request_id,
        deps=DiscordInteractionDispatchDeps(
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
            task_scheduler=asyncio.create_task,
            logger=logger,
        ),
    )
