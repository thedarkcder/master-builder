from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from sqlalchemy.orm import Session

from orchestrator.core.platform_team_catalog_service import platform_team_catalog_service


class AgentRuntimeRoutingValidationError(ValueError):
    pass


@dataclass(frozen=True)
class RuntimeBindingCatalog:
    available_roles: set[str]
    available_named_agents: set[str]
    available_selectors: set[str]


def canonicalize_selector_routing(selector_routing: dict[str, str]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for selector, profile_name in selector_routing.items():
        canonical_selector = "discord.voice_entry_router" if selector == "discord.voice_room_router" else selector
        normalized[canonical_selector] = profile_name
    return normalized


def load_runtime_binding_catalog(*, session: Session) -> RuntimeBindingCatalog:
    bindings = platform_team_catalog_service.list_runtime_bindings(session=session)
    return RuntimeBindingCatalog(
        available_roles=set(bindings["available_roles"]),
        available_named_agents=set(bindings["available_named_agents"]),
        available_selectors=set(bindings["available_selectors"]),
    )


def validate_runtime_routing_payload(
    *,
    role_routing: dict[str, str],
    name_routing: dict[str, str],
    selector_routing: dict[str, str],
    available_profiles: Iterable[str],
    bindings: RuntimeBindingCatalog,
) -> None:
    known_profiles = set(available_profiles)

    for role, profile_name in role_routing.items():
        if role not in bindings.available_roles:
            raise AgentRuntimeRoutingValidationError(f"Unknown agent role: {role}")
        if profile_name not in known_profiles:
            raise AgentRuntimeRoutingValidationError(f"Unknown execution profile: {profile_name}")
    for agent_name, profile_name in name_routing.items():
        if agent_name not in bindings.available_named_agents:
            raise AgentRuntimeRoutingValidationError(f"Unknown named agent: {agent_name}")
        if profile_name not in known_profiles:
            raise AgentRuntimeRoutingValidationError(f"Unknown execution profile: {profile_name}")
    for selector, profile_name in selector_routing.items():
        if selector not in bindings.available_selectors:
            raise AgentRuntimeRoutingValidationError(f"Unknown selector: {selector}")
        if profile_name not in known_profiles:
            raise AgentRuntimeRoutingValidationError(f"Unknown execution profile: {profile_name}")
