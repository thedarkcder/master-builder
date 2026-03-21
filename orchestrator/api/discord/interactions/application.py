from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import replace

from fastapi import HTTPException
from sqlalchemy.orm import Session

from orchestrator.api.discord.interactions.dispatcher import (
    DiscordInteractionDispatchDeps,
    dispatch_discord_interaction,
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


def require_discord_interaction_context(action: DiscordInteractionResponseAction) -> tuple[str, str]:
    interaction_id = str(action.interaction_id or "").strip()
    interaction_token = str(action.interaction_token or "").strip()
    if not interaction_id or not interaction_token:
        raise HTTPException(status_code=400, detail="Missing Discord interaction context")
    return interaction_id, interaction_token
