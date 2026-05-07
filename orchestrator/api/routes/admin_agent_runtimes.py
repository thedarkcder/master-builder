from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from orchestrator.api.dependencies import get_session
from orchestrator.api.schemas import (
    AgentExecutionProfileCreate,
    AgentExecutionProfileRead,
    AgentExecutionProfilesRead,
    AgentExecutionProfileWrite,
    AgentRuntimeToolRead,
    AgentRuntimeToolsRead,
    AgentRuntimeRoutingDefaultsRead,
    AgentRuntimeRoutingRead,
    AgentRuntimeRoutingUpdate,
)
from orchestrator.core.runtime.tools import TOOL_ALLOWLIST, list_implemented_tools
from orchestrator.core.runtime.agent_execution_profiles import (
    AgentExecutionProfile,
    build_agent_execution_profile,
    collect_models_for_runtime_kind,
    default_agent_name_routing,
    default_agent_role_routing,
    default_execution_profiles,
    default_execution_profile_routing,
    list_known_execution_selectors,
    list_known_agent_names,
    list_known_agent_roles,
    normalize_execution_profile_routing,
    normalize_agent_routing,
    normalize_execution_profiles,
    runtime_kind_requires_base_url,
    runtime_kind_supports_api_key,
    runtime_kind_supports_reasoning_effort,
)
from orchestrator.core.config import get_settings
from orchestrator.core.platform.settings_service import (
    SETTING_KEY_AGENT_RUNTIME_PROFILES,
    SETTING_KEY_AGENT_RUNTIME_ROUTING,
    platform_settings_service,
)
from orchestrator.core.security import require_admin

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _canonicalize_selector_routing(selector_routing: dict[str, str]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for selector, profile_name in selector_routing.items():
        canonical_selector = "discord.voice_entry_router" if selector == "discord.voice_room_router" else selector
        normalized[canonical_selector] = profile_name
    return normalized


def _default_profiles() -> dict[str, dict]:
    settings = get_settings()
    return default_execution_profiles(
        default_codex_cli_command=settings.codex_cli_command,
        default_codex_reasoning_effort=settings.codex_reasoning_effort,
        default_codex_supported_models=settings.codex_supported_models,
        default_chat_cli_command=settings.chat_cli_command,
        default_chat_reasoning_effort=settings.chat_reasoning_effort,
        default_claude_cli_command=getattr(settings, "claude_cli_command", ""),
    )


def _configured_profiles(*, session: Session) -> dict[str, dict]:
    payload = platform_settings_service.get_json(session=session, setting_key=SETTING_KEY_AGENT_RUNTIME_PROFILES)
    return normalize_execution_profiles(payload.get("profiles"))


def _save_configured_profiles(*, session: Session, profiles: dict[str, dict]) -> None:
    normalized = normalize_execution_profiles(profiles)
    if normalized:
        platform_settings_service.upsert_json(
            session=session,
            setting_key=SETTING_KEY_AGENT_RUNTIME_PROFILES,
            value_json={"profiles": normalized},
        )
        return
    platform_settings_service.delete(session=session, setting_key=SETTING_KEY_AGENT_RUNTIME_PROFILES)


def _merged_profiles(*, session: Session) -> tuple[dict[str, dict], dict[str, dict], dict[str, dict]]:
    defaults = _default_profiles()
    configured = _configured_profiles(session=session)
    merged = dict(defaults)
    merged.update(configured)
    return defaults, configured, merged


def _current_routing(*, session: Session) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    payload = platform_settings_service.get_json(session=session, setting_key=SETTING_KEY_AGENT_RUNTIME_ROUTING)
    selector_routing = _canonicalize_selector_routing(normalize_execution_profile_routing(payload.get("selector_routing")))
    return (
        normalize_agent_routing(payload.get("role_routing")),
        normalize_agent_routing(payload.get("name_routing")),
        selector_routing,
    )


def _profile_usage_references(
    *,
    profile_name: str,
    defaults: dict[str, dict],
    configured: dict[str, dict],
    role_routing: dict[str, str],
    name_routing: dict[str, str],
    selector_routing: dict[str, str],
) -> list[str]:
    references: list[str] = []
    merged_role_routing = default_agent_role_routing()
    merged_role_routing.update(role_routing)
    for role, selected_profile in merged_role_routing.items():
        if selected_profile == profile_name:
            references.append(f"role:{role}")

    merged_name_routing = default_agent_name_routing()
    merged_name_routing.update(name_routing)
    for agent_name, selected_profile in merged_name_routing.items():
        if selected_profile == profile_name:
            references.append(f"named-agent:{agent_name}")

    merged_selector_routing = default_execution_profile_routing()
    merged_selector_routing.update(selector_routing)
    for selector, selected_profile in merged_selector_routing.items():
        if selected_profile == profile_name:
            references.append(f"selector:{selector}")

    merged_profiles = dict(defaults)
    merged_profiles.update(configured)
    for other_profile_name, raw_profile in merged_profiles.items():
        fallback_profile = str(raw_profile.get("fallback_profile") or "").strip()
        if fallback_profile == profile_name:
            references.append(f"fallback:{other_profile_name}")
    return references


def _profile_to_read(
    *,
    profile_name: str,
    profile: AgentExecutionProfile,
    defaults: dict[str, dict],
    configured: dict[str, dict],
    role_routing: dict[str, str],
    name_routing: dict[str, str],
    selector_routing: dict[str, str],
) -> AgentExecutionProfileRead:
    is_builtin = profile_name in defaults
    is_overridden = profile_name in configured
    usage_references = _profile_usage_references(
        profile_name=profile_name,
        defaults=defaults,
        configured=configured,
        role_routing=role_routing,
        name_routing=name_routing,
        selector_routing=selector_routing,
    )
    return AgentExecutionProfileRead(
        profile_name=profile.profile_name,
        runtime_kind=profile.runtime_kind,
        cli_command=profile.cli_command,
        model=profile.model,
        reasoning_effort=profile.reasoning_effort,
        tool_bridge_allowed=profile.tool_bridge_allowed,
        fallback_profile=profile.fallback_profile,
        base_url=profile.base_url,
        api_key_secret_ref=profile.api_key_secret_ref,
        is_builtin=is_builtin,
        is_overridden=is_overridden,
        can_delete=not is_builtin and not usage_references,
        can_reset=is_builtin and is_overridden,
        usage_references=usage_references,
    )


def _available_profiles(*, session: Session) -> dict[str, AgentExecutionProfileRead]:
    defaults, configured, merged = _merged_profiles(session=session)
    role_routing, name_routing, selector_routing = _current_routing(session=session)
    available: dict[str, AgentExecutionProfileRead] = {}
    for profile_name in sorted(merged.keys()):
        profile = build_agent_execution_profile(profile_name=profile_name, profiles=merged)
        available[profile_name] = _profile_to_read(
            profile_name=profile_name,
            profile=profile,
            defaults=defaults,
            configured=configured,
            role_routing=role_routing,
            name_routing=name_routing,
            selector_routing=selector_routing,
        )
    return available


def _validate_routing_payload(
    *,
    role_routing: dict[str, str],
    name_routing: dict[str, str],
    selector_routing: dict[str, str],
    available_profiles: dict[str, AgentExecutionProfileRead],
) -> None:
    known_roles = set(list_known_agent_roles())
    known_names = set(list_known_agent_names())
    known_selectors = set(list_known_execution_selectors())
    known_profiles = set(available_profiles.keys())

    for role, profile_name in role_routing.items():
        if role not in known_roles:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unknown agent role: {role}")
        if profile_name not in known_profiles:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unknown execution profile: {profile_name}")
    for agent_name, profile_name in name_routing.items():
        if agent_name not in known_names:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unknown named agent: {agent_name}")
        if profile_name not in known_profiles:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unknown execution profile: {profile_name}")
    for selector, profile_name in selector_routing.items():
        if selector not in known_selectors:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unknown selector: {selector}")
        if profile_name not in known_profiles:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unknown execution profile: {profile_name}")


def _normalize_profile_payload(payload: AgentExecutionProfileWrite) -> dict[str, object]:
    return {
        "runtime_kind": payload.runtime_kind,
        "cli_command": payload.cli_command,
        "model": payload.model,
        "reasoning_effort": payload.reasoning_effort,
        "tool_bridge_allowed": payload.tool_bridge_allowed,
        "fallback_profile": payload.fallback_profile,
        "base_url": payload.base_url,
        "api_key_secret_ref": payload.api_key_secret_ref,
    }


def _validate_profile_write(
    *,
    profile_name: str,
    payload: AgentExecutionProfileWrite,
    defaults: dict[str, dict],
    configured: dict[str, dict],
    merged: dict[str, dict],
    creating: bool,
) -> dict[str, dict]:
    normalized_name = str(profile_name or "").strip()
    if not normalized_name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="profile_name is required")

    raw_profiles = dict(configured)
    normalized_profiles = normalize_execution_profiles(
        {
            **raw_profiles,
            normalized_name: _normalize_profile_payload(payload),
        }
    )
    if normalized_name not in normalized_profiles:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid execution profile payload")

    runtime_kind = normalized_profiles[normalized_name]["runtime_kind"]
    cli_command = str(normalized_profiles[normalized_name].get("cli_command") or "").strip()
    base_url = str(normalized_profiles[normalized_name].get("base_url") or "").strip()
    api_key_secret_ref = str(normalized_profiles[normalized_name].get("api_key_secret_ref") or "").strip()
    fallback_profile = str(normalized_profiles[normalized_name].get("fallback_profile") or "").strip()

    if runtime_kind in {"codex_cli", "chat_cli", "claude_cli"} and not cli_command:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="cli_command is required for CLI runtimes")
    if runtime_kind_requires_base_url(runtime_kind) and not base_url:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"base_url is required for runtime {runtime_kind}")
    if runtime_kind in {"openai", "claude"} and not api_key_secret_ref:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"api_key_secret_ref is required for runtime {runtime_kind}")
    if not runtime_kind_supports_api_key(runtime_kind) and api_key_secret_ref:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"runtime {runtime_kind} does not use api_key_secret_ref")
    if not runtime_kind_supports_reasoning_effort(runtime_kind) and payload.reasoning_effort is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"runtime {runtime_kind} does not support reasoning_effort")
    if creating and normalized_name in defaults:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Built-in profile already exists: {normalized_name}")
    if creating and normalized_name in configured:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Execution profile already exists: {normalized_name}")
    if fallback_profile and fallback_profile == normalized_name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="fallback_profile cannot point to itself")

    candidate_profiles = dict(merged)
    candidate_profiles[normalized_name] = normalized_profiles[normalized_name]
    if fallback_profile and fallback_profile not in candidate_profiles:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unknown fallback profile: {fallback_profile}")
    return normalized_profiles


def _build_routing_response(*, session: Session) -> AgentRuntimeRoutingRead:
    role_routing, name_routing, selector_routing = _current_routing(session=session)
    return AgentRuntimeRoutingRead(
        role_routing=role_routing,
        name_routing=name_routing,
        selector_routing=selector_routing,
        available_roles=list_known_agent_roles(),
        available_named_agents=list_known_agent_names(),
        available_selectors=list_known_execution_selectors(),
        available_profiles=_available_profiles(session=session),
        effective_defaults=AgentRuntimeRoutingDefaultsRead(
            role_routing=default_agent_role_routing(),
            name_routing=default_agent_name_routing(),
            selector_routing=default_execution_profile_routing(),
        ),
    )


@router.get("/agent-runtimes", response_model=AgentRuntimeRoutingRead)
def get_agent_runtimes(
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> AgentRuntimeRoutingRead:
    return _build_routing_response(session=session)


@router.put("/agent-runtimes", response_model=AgentRuntimeRoutingRead)
def put_agent_runtimes(
    payload: AgentRuntimeRoutingUpdate,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> AgentRuntimeRoutingRead:
    available_profiles = _available_profiles(session=session)
    role_routing = normalize_agent_routing(payload.role_routing)
    name_routing = normalize_agent_routing(payload.name_routing)
    selector_routing = _canonicalize_selector_routing(normalize_execution_profile_routing(payload.selector_routing))
    _validate_routing_payload(
        role_routing=role_routing,
        name_routing=name_routing,
        selector_routing=selector_routing,
        available_profiles=available_profiles,
    )
    platform_settings_service.upsert_json(
        session=session,
        setting_key=SETTING_KEY_AGENT_RUNTIME_ROUTING,
        value_json={
            "role_routing": role_routing,
            "name_routing": name_routing,
            "selector_routing": selector_routing,
        },
    )
    return _build_routing_response(session=session)


@router.post("/agent-runtimes/reset", response_model=AgentRuntimeRoutingRead)
def reset_agent_runtimes(
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> AgentRuntimeRoutingRead:
    platform_settings_service.delete(session=session, setting_key=SETTING_KEY_AGENT_RUNTIME_ROUTING)
    return _build_routing_response(session=session)


@router.get("/agent-runtime-profiles", response_model=AgentExecutionProfilesRead)
def get_agent_runtime_profiles(
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> AgentExecutionProfilesRead:
    return AgentExecutionProfilesRead(profiles=_available_profiles(session=session))


@router.get("/agent-runtime-tools", response_model=AgentRuntimeToolsRead)
def get_agent_runtime_tools(
    _: str = Depends(require_admin),
) -> AgentRuntimeToolsRead:
    return AgentRuntimeToolsRead(
        available_stages=list(TOOL_ALLOWLIST.keys()),
        tools=[AgentRuntimeToolRead.model_validate(tool) for tool in list_implemented_tools()],
    )


@router.post("/agent-runtime-profiles", response_model=AgentExecutionProfileRead)
def create_agent_runtime_profile(
    payload: AgentExecutionProfileCreate,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> AgentExecutionProfileRead:
    defaults, configured, merged = _merged_profiles(session=session)
    normalized_profiles = _validate_profile_write(
        profile_name=payload.profile_name,
        payload=payload,
        defaults=defaults,
        configured=configured,
        merged=merged,
        creating=True,
    )
    configured[payload.profile_name] = normalized_profiles[payload.profile_name]
    _save_configured_profiles(session=session, profiles=configured)
    return _available_profiles(session=session)[payload.profile_name]


@router.put("/agent-runtime-profiles/{profile_name}", response_model=AgentExecutionProfileRead)
def update_agent_runtime_profile(
    profile_name: str,
    payload: AgentExecutionProfileWrite,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> AgentExecutionProfileRead:
    defaults, configured, merged = _merged_profiles(session=session)
    normalized_name = str(profile_name or "").strip()
    if normalized_name not in merged:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Execution profile not found")
    normalized_profiles = _validate_profile_write(
        profile_name=normalized_name,
        payload=payload,
        defaults=defaults,
        configured=configured,
        merged=merged,
        creating=False,
    )
    configured[normalized_name] = normalized_profiles[normalized_name]
    _save_configured_profiles(session=session, profiles=configured)
    return _available_profiles(session=session)[normalized_name]


@router.post("/agent-runtime-profiles/{profile_name}/reset", response_model=AgentExecutionProfileRead)
def reset_agent_runtime_profile(
    profile_name: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> AgentExecutionProfileRead:
    defaults, configured, merged = _merged_profiles(session=session)
    normalized_name = str(profile_name or "").strip()
    if normalized_name not in defaults:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only built-in profiles can be reset")
    if normalized_name not in merged:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Execution profile not found")
    configured.pop(normalized_name, None)
    _save_configured_profiles(session=session, profiles=configured)
    return _available_profiles(session=session)[normalized_name]


@router.delete("/agent-runtime-profiles/{profile_name}", response_model=AgentExecutionProfilesRead)
def delete_agent_runtime_profile(
    profile_name: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> AgentExecutionProfilesRead:
    defaults, configured, _merged = _merged_profiles(session=session)
    normalized_name = str(profile_name or "").strip()
    if normalized_name in defaults:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Built-in profiles cannot be deleted")
    if normalized_name not in configured:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Execution profile not found")

    available_profile = _available_profiles(session=session).get(normalized_name)
    if available_profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Execution profile not found")
    if available_profile.usage_references:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Profile is still in use: {', '.join(available_profile.usage_references)}",
        )

    configured.pop(normalized_name, None)
    _save_configured_profiles(session=session, profiles=configured)
    return AgentExecutionProfilesRead(profiles=_available_profiles(session=session))


@router.get("/agent-runtime-models")
def list_agent_runtime_models(
    runtime_kind: str | None = Query(default=None),
    profile_name: str | None = Query(default=None),
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> dict[str, object]:
    settings = get_settings()
    defaults, _configured, merged = _merged_profiles(session=session)
    normalized_profile_name = str(profile_name or "").strip() or None
    if normalized_profile_name is not None and normalized_profile_name not in merged:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Execution profile not found")
    resolved_profile = (
        build_agent_execution_profile(profile_name=normalized_profile_name, profiles=merged)
        if normalized_profile_name
        else None
    )
    resolved_runtime_kind = (
        resolved_profile.runtime_kind
        if resolved_profile is not None
        else str(runtime_kind or "codex_cli").strip().lower()
    )
    options = collect_models_for_runtime_kind(
        runtime_kind=resolved_runtime_kind,
        default_model=resolved_profile.model if resolved_profile is not None else None,
        codex_supported_models=settings.codex_supported_models,
        profiles=merged,
    )
    resolved_default_model = (
        resolved_profile.model
        if resolved_profile is not None
        else (options[0].model_id if options else "")
    )
    reasoning_efforts = []
    if runtime_kind_supports_reasoning_effort(resolved_runtime_kind):
        reasoning_efforts = [
            {"id": "medium", "label": "Medium", "description": "Balanced depth and speed"},
            {"id": "low", "label": "Low", "description": "Fastest responses with less deliberation"},
            {"id": "high", "label": "High", "description": "Most deliberate reasoning mode"},
        ]
    return {
        "default_model": resolved_default_model,
        "default_reasoning_effort": settings.codex_reasoning_effort,
        "runtime_kind": resolved_runtime_kind,
        "profile_name": normalized_profile_name,
        "models": [
            {"id": option.model_id, "label": option.label, "description": option.description}
            for option in options
        ],
        "reasoning_efforts": reasoning_efforts,
    }
