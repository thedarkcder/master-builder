from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from orchestrator.api.dependencies import get_session
from orchestrator.api.schemas import (
    AgentExecutionProfileRead,
    AgentRuntimeRoutingDefaultsRead,
    AgentRuntimeRoutingRead,
    AgentRuntimeRoutingUpdate,
)
from orchestrator.core.agent_execution_profiles import (
    build_agent_execution_profile,
    default_agent_name_routing,
    default_agent_role_routing,
    default_execution_profiles,
    list_known_agent_names,
    list_known_agent_roles,
    normalize_agent_routing,
)
from orchestrator.core.config import get_settings
from orchestrator.core.platform_settings_service import (
    SETTING_KEY_AGENT_RUNTIME_ROUTING,
    platform_settings_service,
)
from orchestrator.core.security import require_admin

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _available_profiles() -> dict[str, AgentExecutionProfileRead]:
    settings = get_settings()
    raw_profiles = default_execution_profiles(
        default_codex_cli_command=settings.codex_cli_command,
        default_codex_model=settings.codex_model,
        default_codex_reasoning_effort=settings.codex_reasoning_effort,
        default_codex_supported_models=settings.codex_supported_models,
        default_chat_cli_command=settings.chat_cli_command,
        default_chat_model=settings.chat_model,
        default_chat_reasoning_effort=settings.chat_reasoning_effort,
    )
    available: dict[str, AgentExecutionProfileRead] = {}
    for profile_name in sorted(raw_profiles.keys()):
        profile = build_agent_execution_profile(profile_name=profile_name, profiles=raw_profiles)
        available[profile_name] = AgentExecutionProfileRead(
            profile_name=profile.profile_name,
            runtime_kind=profile.runtime_kind,
            cli_command=profile.cli_command,
            model=profile.model,
            reasoning_effort=profile.reasoning_effort,
            tool_bridge_allowed=profile.tool_bridge_allowed,
            fallback_profile=profile.fallback_profile,
        )
    return available


def _current_routing(*, session: Session) -> tuple[dict[str, str], dict[str, str]]:
    payload = platform_settings_service.get_json(session=session, setting_key=SETTING_KEY_AGENT_RUNTIME_ROUTING)
    return (
        normalize_agent_routing(payload.get("role_routing")),
        normalize_agent_routing(payload.get("name_routing")),
    )


def _validate_payload(payload: AgentRuntimeRoutingUpdate, *, available_profiles: dict[str, AgentExecutionProfileRead]) -> None:
    known_roles = set(list_known_agent_roles())
    known_names = set(list_known_agent_names())
    known_profiles = set(available_profiles.keys())

    for role, profile_name in payload.role_routing.items():
        if role not in known_roles:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unknown agent role: {role}")
        if profile_name not in known_profiles:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unknown execution profile: {profile_name}")
    for agent_name, profile_name in payload.name_routing.items():
        if agent_name not in known_names:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unknown named agent: {agent_name}")
        if profile_name not in known_profiles:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unknown execution profile: {profile_name}")


def _build_response(*, session: Session) -> AgentRuntimeRoutingRead:
    role_routing, name_routing = _current_routing(session=session)
    return AgentRuntimeRoutingRead(
        role_routing=role_routing,
        name_routing=name_routing,
        available_roles=list_known_agent_roles(),
        available_named_agents=list_known_agent_names(),
        available_profiles=_available_profiles(),
        effective_defaults=AgentRuntimeRoutingDefaultsRead(
            role_routing=default_agent_role_routing(),
            name_routing=default_agent_name_routing(),
        ),
    )


@router.get("/agent-runtimes", response_model=AgentRuntimeRoutingRead)
def get_agent_runtimes(
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> AgentRuntimeRoutingRead:
    return _build_response(session=session)


@router.put("/agent-runtimes", response_model=AgentRuntimeRoutingRead)
def put_agent_runtimes(
    payload: AgentRuntimeRoutingUpdate,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> AgentRuntimeRoutingRead:
    available_profiles = _available_profiles()
    _validate_payload(payload, available_profiles=available_profiles)
    platform_settings_service.upsert_json(
        session=session,
        setting_key=SETTING_KEY_AGENT_RUNTIME_ROUTING,
        value_json={
            "role_routing": normalize_agent_routing(payload.role_routing),
            "name_routing": normalize_agent_routing(payload.name_routing),
        },
    )
    return _build_response(session=session)


@router.post("/agent-runtimes/reset", response_model=AgentRuntimeRoutingRead)
def reset_agent_runtimes(
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> AgentRuntimeRoutingRead:
    platform_settings_service.delete(
        session=session,
        setting_key=SETTING_KEY_AGENT_RUNTIME_ROUTING,
    )
    return _build_response(session=session)
