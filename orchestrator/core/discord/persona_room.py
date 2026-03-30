from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from orchestrator.core.codex_agents import (
    answer_voice_room_persona_with_codex,
    route_voice_room_persona_with_codex,
)
from orchestrator.core.codex_invocation import CodexInvocationContext
from orchestrator.core.codex_runtime import CodexRuntime
from orchestrator.core.discord.personas import (
    build_voice_room_config,
    get_voice_room_persona_definition,
    list_voice_room_personas,
    resolve_voice_room_persona_profile,
)


@dataclass(frozen=True)
class VoiceRoomTurnResult:
    persona_id: str
    persona_role: str
    persona_name: str
    persona_voice_id: str | None
    message: str
    brief: dict[str, Any]
    router_confidence: float
    router_reason: str
    room_config: dict[str, dict[str, str]]


def answer_voice_room_turn(
    *,
    runtime: CodexRuntime,
    runtime_for_selector: Any | None = None,
    transcript: str,
    project_keys: list[str],
    issues: list[dict],
    status_counts: dict[str, int],
    invocation_context: CodexInvocationContext,
    history: list[dict] | None = None,
    github_context: dict | None = None,
    tenant_discord_config: dict | None = None,
    project_discord_config: dict | None = None,
    sqlalchemy_session: Any | None = None,
    settings: Any | None = None,
) -> VoiceRoomTurnResult:
    def _runtime(selector: str) -> CodexRuntime:
        if callable(runtime_for_selector):
            resolved = runtime_for_selector(selector)
            if isinstance(resolved, CodexRuntime):
                return resolved
        return runtime

    room_config = build_voice_room_config(tenant_discord_config, project_discord_config)
    router_payload = route_voice_room_persona_with_codex(
        runtime=_runtime("discord.voice_room_router"),
        transcript=transcript,
        available_personas=list_voice_room_personas(),
        invocation_context=replace(invocation_context, stage="voice-room-router"),
        history=history,
        room_context=_build_room_context(
            project_keys=project_keys,
            issues=issues,
            status_counts=status_counts,
            github_context=github_context,
        ),
    )
    persona_id = str(router_payload.get("persona") or "").strip().lower()
    persona = get_voice_room_persona_definition(persona_id)
    persona_profile = resolve_voice_room_persona_profile(
        persona_id=persona.persona_id,
        tenant_discord_config=tenant_discord_config,
        project_discord_config=project_discord_config,
    )
    answer_payload = answer_voice_room_persona_with_codex(
        runtime=_runtime(f"discord.voice_room_{persona.persona_id}"),
        persona_id=persona.persona_id,
        transcript=transcript,
        project_keys=project_keys,
        issues=issues,
        status_counts=status_counts,
        invocation_context=replace(invocation_context, stage=f"voice-room-{persona.persona_id}"),
        history=history,
        github_context=github_context,
        room_context=_build_room_context(
            project_keys=project_keys,
            issues=issues,
            status_counts=status_counts,
            github_context=github_context,
        ),
        sqlalchemy_session=sqlalchemy_session,
        settings=settings,
    )
    return VoiceRoomTurnResult(
        persona_id=persona.persona_id,
        persona_role=persona_profile.role_label,
        persona_name=persona_profile.display_name,
        persona_voice_id=persona_profile.voice_id,
        message=str(answer_payload.get("message") or "").strip(),
        brief=answer_payload.get("brief") if isinstance(answer_payload.get("brief"), dict) else {},
        router_confidence=_normalize_confidence(router_payload.get("confidence")),
        router_reason=str(router_payload.get("reason") or "").strip(),
        room_config=room_config,
    )


def _build_room_context(
    *,
    project_keys: list[str],
    issues: list[dict],
    status_counts: dict[str, int],
    github_context: dict | None,
) -> dict[str, Any]:
    return {
        "project_keys": list(project_keys),
        "status_counts": dict(status_counts),
        "github_context": github_context or {},
        "issues": issues[:40],
    }


def _normalize_confidence(value: object) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.0
    if confidence < 0:
        return 0.0
    if confidence > 1:
        return 1.0
    return confidence
