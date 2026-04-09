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
    AgentRuntimeRoutingRead,
    AgentRuntimeRoutingUpdate,
)
from orchestrator.core.agent_execution_profiles import normalize_agent_routing, normalize_execution_profile_routing
from orchestrator.core.agent_runtime_routing_policy import canonicalize_selector_routing
from orchestrator.core.agent_tools import TOOL_ALLOWLIST, list_implemented_tools
from orchestrator.core.runtime_config_application_service import (
    RuntimeConfigValidationError,
    runtime_config_application_service,
)
from orchestrator.core.security import require_admin

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _routing_response_from_payload(payload: dict[str, object]) -> AgentRuntimeRoutingRead:
    profile_payloads = payload.get("available_profiles")
    profile_map: dict[str, AgentExecutionProfileRead] = {}
    if isinstance(profile_payloads, dict):
        for profile_name, profile_payload in profile_payloads.items():
            if isinstance(profile_payload, dict):
                profile_map[str(profile_name)] = AgentExecutionProfileRead.model_validate(profile_payload)
    response_payload = dict(payload)
    response_payload["available_profiles"] = profile_map
    return AgentRuntimeRoutingRead.model_validate(response_payload)


def _profile_map_response(*, profiles: dict[str, dict[str, object]]) -> AgentExecutionProfilesRead:
    return AgentExecutionProfilesRead(
        profiles={
            profile_name: AgentExecutionProfileRead.model_validate(profile_payload)
            for profile_name, profile_payload in profiles.items()
        }
    )


@router.get("/agent-runtimes", response_model=AgentRuntimeRoutingRead)
def get_agent_runtimes(
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> AgentRuntimeRoutingRead:
    payload = runtime_config_application_service.build_routing_response(session=session)
    return _routing_response_from_payload(payload)


@router.put("/agent-runtimes", response_model=AgentRuntimeRoutingRead)
def put_agent_runtimes(
    payload: AgentRuntimeRoutingUpdate,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> AgentRuntimeRoutingRead:
    available_profiles = runtime_config_application_service.available_profiles(session=session)
    role_routing = normalize_agent_routing(payload.role_routing)
    name_routing = normalize_agent_routing(payload.name_routing)
    selector_routing = canonicalize_selector_routing(normalize_execution_profile_routing(payload.selector_routing))
    try:
        runtime_config_application_service.validate_routing_payload(
            session=session,
            role_routing=role_routing,
            name_routing=name_routing,
            selector_routing=selector_routing,
            available_profiles=available_profiles,
        )
        runtime_config_application_service.put_routing(
            session=session,
            role_routing=role_routing,
            name_routing=name_routing,
            selector_routing=selector_routing,
        )
    except RuntimeConfigValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return _routing_response_from_payload(runtime_config_application_service.build_routing_response(session=session))


@router.post("/agent-runtimes/reset", response_model=AgentRuntimeRoutingRead)
def reset_agent_runtimes(
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> AgentRuntimeRoutingRead:
    runtime_config_application_service.reset_routing(session=session)
    return _routing_response_from_payload(runtime_config_application_service.build_routing_response(session=session))


@router.get("/agent-runtime-profiles", response_model=AgentExecutionProfilesRead)
def get_agent_runtime_profiles(
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> AgentExecutionProfilesRead:
    return _profile_map_response(profiles=runtime_config_application_service.available_profiles(session=session))


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
    defaults, configured, merged = runtime_config_application_service.merged_profiles(session=session)
    try:
        normalized_profiles = runtime_config_application_service.validate_profile_write(
            profile_name=payload.profile_name,
            payload=payload,
            defaults=defaults,
            configured=configured,
            merged=merged,
            creating=True,
        )
    except RuntimeConfigValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    configured[payload.profile_name] = normalized_profiles[payload.profile_name]
    runtime_config_application_service.save_configured_profiles(session=session, profiles=configured)
    available = runtime_config_application_service.available_profiles(session=session)
    return AgentExecutionProfileRead.model_validate(available[payload.profile_name])


@router.put("/agent-runtime-profiles/{profile_name}", response_model=AgentExecutionProfileRead)
def update_agent_runtime_profile(
    profile_name: str,
    payload: AgentExecutionProfileWrite,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> AgentExecutionProfileRead:
    defaults, configured, merged = runtime_config_application_service.merged_profiles(session=session)
    normalized_name = str(profile_name or "").strip()
    if normalized_name not in merged:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Execution profile not found")
    try:
        normalized_profiles = runtime_config_application_service.validate_profile_write(
            profile_name=normalized_name,
            payload=payload,
            defaults=defaults,
            configured=configured,
            merged=merged,
            creating=False,
        )
    except RuntimeConfigValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    configured[normalized_name] = normalized_profiles[normalized_name]
    runtime_config_application_service.save_configured_profiles(session=session, profiles=configured)
    available = runtime_config_application_service.available_profiles(session=session)
    return AgentExecutionProfileRead.model_validate(available[normalized_name])


@router.post("/agent-runtime-profiles/{profile_name}/reset", response_model=AgentExecutionProfileRead)
def reset_agent_runtime_profile(
    profile_name: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> AgentExecutionProfileRead:
    defaults, configured, merged = runtime_config_application_service.merged_profiles(session=session)
    normalized_name = str(profile_name or "").strip()
    if normalized_name not in defaults:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only built-in profiles can be reset")
    if normalized_name not in merged:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Execution profile not found")
    configured.pop(normalized_name, None)
    runtime_config_application_service.save_configured_profiles(session=session, profiles=configured)
    available = runtime_config_application_service.available_profiles(session=session)
    return AgentExecutionProfileRead.model_validate(available[normalized_name])


@router.delete("/agent-runtime-profiles/{profile_name}", response_model=AgentExecutionProfilesRead)
def delete_agent_runtime_profile(
    profile_name: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> AgentExecutionProfilesRead:
    defaults, configured, _merged = runtime_config_application_service.merged_profiles(session=session)
    normalized_name = str(profile_name or "").strip()
    if normalized_name in defaults:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Built-in profiles cannot be deleted")
    if normalized_name not in configured:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Execution profile not found")

    available_profile = runtime_config_application_service.available_profiles(session=session).get(normalized_name)
    if available_profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Execution profile not found")
    usage_references = available_profile.get("usage_references")
    if isinstance(usage_references, list) and usage_references:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Profile is still in use: {', '.join(str(item) for item in usage_references)}",
        )

    configured.pop(normalized_name, None)
    runtime_config_application_service.save_configured_profiles(session=session, profiles=configured)
    return _profile_map_response(profiles=runtime_config_application_service.available_profiles(session=session))


@router.get("/agent-runtime-models")
def list_agent_runtime_models(
    runtime_kind: str | None = Query(default=None),
    profile_name: str | None = Query(default=None),
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> dict[str, object]:
    try:
        return runtime_config_application_service.list_models(
            session=session,
            runtime_kind=runtime_kind,
            profile_name=profile_name,
        )
    except RuntimeConfigValidationError as exc:
        detail = str(exc)
        status_code = status.HTTP_404_NOT_FOUND if "not found" in detail.lower() else status.HTTP_400_BAD_REQUEST
        raise HTTPException(status_code=status_code, detail=detail) from exc
