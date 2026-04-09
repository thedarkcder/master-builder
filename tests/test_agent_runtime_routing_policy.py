from __future__ import annotations

import pytest

from orchestrator.core.agent_runtime_routing_policy import (
    AgentRuntimeRoutingValidationError,
    RuntimeBindingCatalog,
    canonicalize_selector_routing,
    validate_runtime_routing_payload,
)


def test_canonicalize_selector_routing_normalizes_voice_room_router() -> None:
    normalized = canonicalize_selector_routing(
        {
            "discord.voice_room_router": "general_planning_default",
            "team.launch.strategy": "general_planning_default",
        }
    )
    assert normalized["discord.voice_entry_router"] == "general_planning_default"
    assert "discord.voice_room_router" not in normalized
    assert normalized["team.launch.strategy"] == "general_planning_default"


def test_validate_runtime_routing_payload_accepts_known_bindings() -> None:
    bindings = RuntimeBindingCatalog(
        available_roles={"launch_strategy"},
        available_named_agents={"launch_strategy_primary"},
        available_selectors={"team.launch.strategy"},
    )
    validate_runtime_routing_payload(
        role_routing={"launch_strategy": "general_planning_default"},
        name_routing={"launch_strategy_primary": "general_planning_default"},
        selector_routing={"team.launch.strategy": "general_planning_default"},
        available_profiles={"general_planning_default"},
        bindings=bindings,
    )


def test_validate_runtime_routing_payload_rejects_unknown_role() -> None:
    bindings = RuntimeBindingCatalog(
        available_roles={"launch_strategy"},
        available_named_agents={"launch_strategy_primary"},
        available_selectors={"team.launch.strategy"},
    )
    with pytest.raises(AgentRuntimeRoutingValidationError, match="Unknown agent role"):
        validate_runtime_routing_payload(
            role_routing={"unknown_role": "general_planning_default"},
            name_routing={},
            selector_routing={},
            available_profiles={"general_planning_default"},
            bindings=bindings,
        )


def test_validate_runtime_routing_payload_rejects_unknown_profile() -> None:
    bindings = RuntimeBindingCatalog(
        available_roles={"launch_strategy"},
        available_named_agents={"launch_strategy_primary"},
        available_selectors={"team.launch.strategy"},
    )
    with pytest.raises(AgentRuntimeRoutingValidationError, match="Unknown execution profile"):
        validate_runtime_routing_payload(
            role_routing={"launch_strategy": "missing_profile"},
            name_routing={},
            selector_routing={},
            available_profiles={"general_planning_default"},
            bindings=bindings,
        )
