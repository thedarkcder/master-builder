from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from orchestrator.core.agent_runtime_resolver import build_runtime_for_selector
from orchestrator.core.codex_agents import route_voice_entry_with_codex
from orchestrator.core.codex_invocation import CodexInvocationContext
from orchestrator.core.codex_runtime import CodexRuntimeError
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
) -> dict[str, Any]:
    """LLM router for voice note / live voice entry; returns lane, persona, confidence, reason."""
    try:
        runtime = build_runtime_for_selector(
            session=session,
            settings=settings,
            tenant_id=tenant.tenant_id,
            project_id=project_id,
            selector="discord.voice_entry_router",
        )
        routed = route_voice_entry_with_codex(
            runtime=runtime,
            transcript=transcript,
            invocation_context=CodexInvocationContext(
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
        )
    except CodexRuntimeError as exc:
        logger.warning(
            "voice_entry_router_failed tenant_id=%s entry_source=%s error=%s",
            tenant.tenant_id,
            entry_source,
            exc,
        )
        return {
            "lane": "ask",
            "persona": "engineer",
            "confidence": 0.0,
            "reason": "router_unavailable",
        }
    logger.info(
        "voice_entry_routed tenant_id=%s entry_source=%s lane=%s persona=%s confidence=%s reason=%s",
        tenant.tenant_id,
        entry_source,
        routed.get("lane"),
        routed.get("persona"),
        routed.get("confidence"),
        routed.get("reason"),
    )
    return routed
