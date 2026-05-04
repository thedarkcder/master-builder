from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from orchestrator.core.runtime.agent_runtime_resolver import build_runtime_for_selector
from orchestrator.core.runtime.agents import route_voice_entry_with_runtime
from orchestrator.core.runtime.invocation import AgentInvocationContext
from orchestrator.core.runtime.payload_models import VoiceEntryRoutePayload
from orchestrator.core.runtime.runtime import CodexRuntimeError
from orchestrator.core.config import Settings
from orchestrator.storage.models import Tenant

logger = logging.getLogger("orchestrator.discord_voice_entry")


def route_discord_voice_entry(
    *,
    session: Session,
    settings: Settings,
    tenant: Tenant,
    project_id: str | None,
    codex_working_dir: str,
    transcript: str,
    entry_source: str,
    history: list[dict[str, Any]] | None = None,
    room_context: dict[str, Any] | None = None,
) -> VoiceEntryRoutePayload:
    """LLM router for voice note / live voice entry; returns lane, persona, confidence, reason."""
    runtime = build_runtime_for_selector(
        session=session,
        settings=settings,
        tenant_id=tenant.tenant_id,
        project_id=project_id,
        selector="discord.voice_entry_router",
    )
    try:
        routed = route_voice_entry_with_runtime(
            runtime=runtime,
            transcript=transcript,
            invocation_context=AgentInvocationContext(
                channel="discord",
                tenant_id=tenant.tenant_id,
                project_id=project_id,
                command="voice_entry_router",
                stage="voice-entry-router",
                working_dir=codex_working_dir,
                issue_key=None,
            ),
            entry_source=entry_source,
            history=history,
            room_context=room_context,
            sqlalchemy_session=session,
            settings=settings,
        )
    except CodexRuntimeError:
        logger.exception(
            "voice_entry_router_failed tenant_id=%s entry_source=%s",
            tenant.tenant_id,
            entry_source,
        )
        raise
    logger.info(
        "voice_entry_routed tenant_id=%s entry_source=%s lane=%s persona=%s confidence=%s reason=%s",
        tenant.tenant_id,
        entry_source,
        routed.lane,
        routed.persona,
        routed.confidence,
        routed.reason,
    )
    return routed
