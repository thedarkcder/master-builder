from __future__ import annotations

from sqlalchemy.orm import Session

from orchestrator.core.agent_execution_profiles import (
    AgentExecutionProfile,
    build_agent_execution_profile,
    collect_models_for_runtime_kind,
    default_agent_name_routing,
    default_agent_role_routing,
    default_execution_profile_routing,
    default_execution_profiles,
    normalize_agent_routing,
    normalize_execution_profile_routing,
    normalize_execution_profiles,
    runtime_kind_requires_base_url,
    runtime_kind_supports_api_key,
    runtime_kind_supports_reasoning_effort,
)
from orchestrator.core.agent_runtime_routing_policy import (
    AgentRuntimeRoutingValidationError,
    canonicalize_selector_routing,
    load_runtime_binding_catalog,
    validate_runtime_routing_payload,
)
from orchestrator.core.config import get_settings
from orchestrator.core.platform_settings_service import (
    SETTING_KEY_AGENT_RUNTIME_PROFILES,
    SETTING_KEY_AGENT_RUNTIME_ROUTING,
    platform_settings_service,
)


class RuntimeConfigValidationError(ValueError):
    pass


class RuntimeConfigApplicationService:
    @staticmethod
    def default_profiles() -> dict[str, dict]:
        settings = get_settings()
        return default_execution_profiles(
            default_codex_cli_command=settings.codex_cli_command,
            default_codex_model=settings.codex_model,
            default_codex_reasoning_effort=settings.codex_reasoning_effort,
            default_codex_supported_models=settings.codex_supported_models,
            default_chat_cli_command=settings.chat_cli_command,
            default_chat_model=settings.chat_model,
            default_chat_reasoning_effort=settings.chat_reasoning_effort,
            default_claude_cli_command=getattr(settings, "claude_cli_command", ""),
        )

    def configured_profiles(self, *, session: Session) -> dict[str, dict]:
        payload = platform_settings_service.get_json(session=session, setting_key=SETTING_KEY_AGENT_RUNTIME_PROFILES)
        return normalize_execution_profiles(payload.get("profiles"))

    def save_configured_profiles(self, *, session: Session, profiles: dict[str, dict]) -> None:
        normalized = normalize_execution_profiles(profiles)
        if normalized:
            platform_settings_service.upsert_json(
                session=session,
                setting_key=SETTING_KEY_AGENT_RUNTIME_PROFILES,
                value_json={"profiles": normalized},
            )
            return
        platform_settings_service.delete(session=session, setting_key=SETTING_KEY_AGENT_RUNTIME_PROFILES)

    def merged_profiles(self, *, session: Session) -> tuple[dict[str, dict], dict[str, dict], dict[str, dict]]:
        defaults = self.default_profiles()
        configured = self.configured_profiles(session=session)
        merged = dict(defaults)
        merged.update(configured)
        return defaults, configured, merged

    def current_routing(self, *, session: Session) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
        payload = platform_settings_service.get_json(session=session, setting_key=SETTING_KEY_AGENT_RUNTIME_ROUTING)
        selector_routing = canonicalize_selector_routing(normalize_execution_profile_routing(payload.get("selector_routing")))
        return (
            normalize_agent_routing(payload.get("role_routing")),
            normalize_agent_routing(payload.get("name_routing")),
            selector_routing,
        )

    @staticmethod
    def profile_usage_references(
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

    def profile_to_read(
        self,
        *,
        profile_name: str,
        profile: AgentExecutionProfile,
        defaults: dict[str, dict],
        configured: dict[str, dict],
        role_routing: dict[str, str],
        name_routing: dict[str, str],
        selector_routing: dict[str, str],
    ) -> dict[str, object]:
        is_builtin = profile_name in defaults
        is_overridden = profile_name in configured
        usage_references = self.profile_usage_references(
            profile_name=profile_name,
            defaults=defaults,
            configured=configured,
            role_routing=role_routing,
            name_routing=name_routing,
            selector_routing=selector_routing,
        )
        return {
            "profile_name": profile.profile_name,
            "runtime_kind": profile.runtime_kind,
            "cli_command": profile.cli_command,
            "model": profile.model,
            "reasoning_effort": profile.reasoning_effort,
            "tool_bridge_allowed": profile.tool_bridge_allowed,
            "fallback_profile": profile.fallback_profile,
            "base_url": profile.base_url,
            "api_key_secret_ref": profile.api_key_secret_ref,
            "is_builtin": is_builtin,
            "is_overridden": is_overridden,
            "can_delete": not is_builtin and not usage_references,
            "can_reset": is_builtin and is_overridden,
            "usage_references": usage_references,
        }

    def available_profiles(self, *, session: Session) -> dict[str, dict[str, object]]:
        defaults, configured, merged = self.merged_profiles(session=session)
        role_routing, name_routing, selector_routing = self.current_routing(session=session)
        available: dict[str, dict[str, object]] = {}
        for profile_name in sorted(merged.keys()):
            profile = build_agent_execution_profile(profile_name=profile_name, profiles=merged)
            available[profile_name] = self.profile_to_read(
                profile_name=profile_name,
                profile=profile,
                defaults=defaults,
                configured=configured,
                role_routing=role_routing,
                name_routing=name_routing,
                selector_routing=selector_routing,
            )
        return available

    @staticmethod
    def normalize_profile_payload(payload) -> dict[str, object]:  # noqa: ANN001
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

    def validate_profile_write(
        self,
        *,
        profile_name: str,
        payload,  # noqa: ANN001
        defaults: dict[str, dict],
        configured: dict[str, dict],
        merged: dict[str, dict],
        creating: bool,
    ) -> dict[str, dict]:
        normalized_name = str(profile_name or "").strip()
        if not normalized_name:
            raise RuntimeConfigValidationError("profile_name is required")

        raw_profiles = dict(configured)
        normalized_profiles = normalize_execution_profiles(
            {
                **raw_profiles,
                normalized_name: self.normalize_profile_payload(payload),
            }
        )
        if normalized_name not in normalized_profiles:
            raise RuntimeConfigValidationError("Invalid execution profile payload")

        runtime_kind = normalized_profiles[normalized_name]["runtime_kind"]
        cli_command = str(normalized_profiles[normalized_name].get("cli_command") or "").strip()
        base_url = str(normalized_profiles[normalized_name].get("base_url") or "").strip()
        api_key_secret_ref = str(normalized_profiles[normalized_name].get("api_key_secret_ref") or "").strip()
        fallback_profile = str(normalized_profiles[normalized_name].get("fallback_profile") or "").strip()

        if runtime_kind in {"codex_cli", "chat_cli", "claude_cli"} and not cli_command:
            raise RuntimeConfigValidationError("cli_command is required for CLI runtimes")
        if runtime_kind_requires_base_url(runtime_kind) and not base_url:
            raise RuntimeConfigValidationError(f"base_url is required for runtime {runtime_kind}")
        if runtime_kind in {"openai", "claude"} and not api_key_secret_ref:
            raise RuntimeConfigValidationError(f"api_key_secret_ref is required for runtime {runtime_kind}")
        if not runtime_kind_supports_api_key(runtime_kind) and api_key_secret_ref:
            raise RuntimeConfigValidationError(f"runtime {runtime_kind} does not use api_key_secret_ref")
        if not runtime_kind_supports_reasoning_effort(runtime_kind) and payload.reasoning_effort is not None:
            raise RuntimeConfigValidationError(f"runtime {runtime_kind} does not support reasoning_effort")
        if creating and normalized_name in defaults:
            raise RuntimeConfigValidationError(f"Built-in profile already exists: {normalized_name}")
        if creating and normalized_name in configured:
            raise RuntimeConfigValidationError(f"Execution profile already exists: {normalized_name}")
        if fallback_profile and fallback_profile == normalized_name:
            raise RuntimeConfigValidationError("fallback_profile cannot point to itself")

        candidate_profiles = dict(merged)
        candidate_profiles[normalized_name] = normalized_profiles[normalized_name]
        if fallback_profile and fallback_profile not in candidate_profiles:
            raise RuntimeConfigValidationError(f"Unknown fallback profile: {fallback_profile}")
        return normalized_profiles

    def build_routing_response(self, *, session: Session) -> dict[str, object]:
        role_routing, name_routing, selector_routing = self.current_routing(session=session)
        bindings = load_runtime_binding_catalog(session=session)
        available_roles = bindings.available_roles
        available_named_agents = bindings.available_named_agents
        available_selectors = bindings.available_selectors
        return {
            "role_routing": role_routing,
            "name_routing": name_routing,
            "selector_routing": selector_routing,
            "available_roles": sorted(available_roles),
            "available_named_agents": sorted(available_named_agents),
            "available_selectors": sorted(available_selectors),
            "available_profiles": self.available_profiles(session=session),
            "effective_defaults": {
                "role_routing": {key: value for key, value in default_agent_role_routing().items() if key in available_roles},
                "name_routing": {key: value for key, value in default_agent_name_routing().items() if key in available_named_agents},
                "selector_routing": {
                    key: value for key, value in default_execution_profile_routing().items() if key in available_selectors
                },
            },
        }

    def validate_routing_payload(
        self,
        *,
        session: Session,
        role_routing: dict[str, str],
        name_routing: dict[str, str],
        selector_routing: dict[str, str],
        available_profiles: dict[str, dict[str, object]],
    ) -> None:
        bindings = load_runtime_binding_catalog(session=session)
        try:
            validate_runtime_routing_payload(
                role_routing=role_routing,
                name_routing=name_routing,
                selector_routing=selector_routing,
                available_profiles=available_profiles.keys(),
                bindings=bindings,
            )
        except AgentRuntimeRoutingValidationError as exc:
            raise RuntimeConfigValidationError(str(exc)) from exc

    def put_routing(
        self,
        *,
        session: Session,
        role_routing: dict[str, str],
        name_routing: dict[str, str],
        selector_routing: dict[str, str],
    ) -> None:
        platform_settings_service.upsert_json(
            session=session,
            setting_key=SETTING_KEY_AGENT_RUNTIME_ROUTING,
            value_json={
                "role_routing": role_routing,
                "name_routing": name_routing,
                "selector_routing": selector_routing,
            },
        )

    def reset_routing(self, *, session: Session) -> None:
        platform_settings_service.delete(session=session, setting_key=SETTING_KEY_AGENT_RUNTIME_ROUTING)

    def list_models(
        self,
        *,
        session: Session,
        runtime_kind: str | None,
        profile_name: str | None,
    ) -> dict[str, object]:
        settings = get_settings()
        _defaults, _configured, merged = self.merged_profiles(session=session)
        normalized_profile_name = str(profile_name or "").strip() or None
        if normalized_profile_name is not None and normalized_profile_name not in merged:
            raise RuntimeConfigValidationError("Execution profile not found")
        resolved_runtime_kind = (
            build_agent_execution_profile(profile_name=normalized_profile_name, profiles=merged).runtime_kind
            if normalized_profile_name
            else str(runtime_kind or "codex_cli").strip().lower()
        )
        resolved_default_model = (
            build_agent_execution_profile(profile_name=normalized_profile_name, profiles=merged).model
            if normalized_profile_name
            else settings.codex_model
        )
        options = collect_models_for_runtime_kind(
            runtime_kind=resolved_runtime_kind,
            default_model=resolved_default_model,
            codex_supported_models=settings.codex_supported_models,
            profiles=merged,
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


runtime_config_application_service = RuntimeConfigApplicationService()
