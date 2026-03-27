from __future__ import annotations

from typing import Any

from orchestrator.core.agent_execution_profiles import (
    AgentExecutionProfile,
    build_agent_execution_profile,
    default_agent_name_routing,
    default_agent_role_routing,
    default_execution_profiles,
    merge_agent_routing,
    merge_execution_profile_routing,
    merge_execution_profiles,
    normalize_agent_routing,
    normalize_execution_profile_routing,
    normalize_execution_profiles,
    resolve_execution_profile_name,
)
from orchestrator.core.codex_runtime import build_runtime_for_execution_profile
from orchestrator.core.codex_runtime import build_runtime_with_fallback
from orchestrator.core.config import Settings
from orchestrator.core.platform_settings_service import (
    SETTING_KEY_AGENT_RUNTIME_ROUTING,
    platform_settings_service,
)
from orchestrator.core.project_policy import resolve_effective_policy
from orchestrator.storage.models import Project, Tenant


def _resolve_agent_execution_profiles(
    *,
    settings: Settings,
    tenant_policy: dict[str, Any] | None,
    project_overrides: dict[str, Any] | None,
    selector: str,
    agent_role: str | None = None,
    agent_name: str | None = None,
    platform_role_routing: dict[str, str] | None = None,
    platform_name_routing: dict[str, str] | None = None,
) -> tuple[AgentExecutionProfile, dict[str, dict[str, Any]]]:
    tenant_profiles = normalize_execution_profiles((tenant_policy or {}).get("execution_profiles"))
    project_profiles = normalize_execution_profiles((project_overrides or {}).get("execution_profiles"))
    effective_policy = resolve_effective_policy(
        tenant_policy=tenant_policy or {},
        project_overrides=project_overrides or {},
        default_codex_model=settings.codex_model,
        default_codex_reasoning_effort=settings.codex_reasoning_effort,
    )
    default_profiles = default_execution_profiles(
        default_codex_cli_command=settings.codex_cli_command,
        default_codex_model=settings.codex_model,
        default_codex_reasoning_effort=settings.codex_reasoning_effort,
        default_codex_supported_models=getattr(settings, "codex_supported_models", None),
        default_chat_cli_command=settings.chat_cli_command,
        default_chat_model=settings.chat_model,
        default_chat_reasoning_effort=settings.chat_reasoning_effort,
    )
    profiles = merge_execution_profiles(
        default_profiles=default_profiles,
        tenant_profiles=tenant_profiles,
        project_profiles=project_profiles,
    )
    routing = merge_execution_profile_routing(
        tenant_routing=normalize_execution_profile_routing((tenant_policy or {}).get("execution_profile_routing")),
        project_routing=normalize_execution_profile_routing((project_overrides or {}).get("execution_profile_routing")),
    )
    # Respect explicit effective-policy overrides as a compatibility path.
    effective_model = str(effective_policy.get("codex_model") or settings.codex_model).strip() or settings.codex_model
    effective_effort = (
        str(effective_policy.get("codex_reasoning_effort") or settings.codex_reasoning_effort).strip().lower()
        or settings.codex_reasoning_effort
    )
    engineering_profile_explicit = any(
        "engineering_execution" in source for source in (tenant_profiles, project_profiles)
    )
    engineering_profile = dict(profiles.get("engineering_execution") or {})
    if engineering_profile and not engineering_profile_explicit:
        engineering_profile["model"] = effective_model
        engineering_profile["reasoning_effort"] = effective_effort
        profiles["engineering_execution"] = engineering_profile
    general_profile_explicit = any(
        "general_planning" in source for source in (tenant_profiles, project_profiles)
    )
    general_profile = dict(profiles.get("general_planning") or {})
    if general_profile and not general_profile_explicit:
        general_profile["model"] = effective_model
        general_profile["reasoning_effort"] = effective_effort
        profiles["general_planning"] = general_profile
    merged_platform_role_routing = merge_agent_routing(
        default_routing=default_agent_role_routing(),
        configured_routing=normalize_agent_routing(platform_role_routing),
    )
    merged_platform_name_routing = merge_agent_routing(
        default_routing=default_agent_name_routing(),
        configured_routing=normalize_agent_routing(platform_name_routing),
    )
    profile_name = resolve_execution_profile_name(
        routing=routing,
        selector=selector,
        agent_role=agent_role,
        agent_name=agent_name,
        platform_role_routing=merged_platform_role_routing,
        platform_name_routing=merged_platform_name_routing,
    )
    return build_agent_execution_profile(profile_name=profile_name, profiles=profiles), profiles


def resolve_agent_execution_profile(
    *,
    settings: Settings,
    tenant_policy: dict[str, Any] | None,
    project_overrides: dict[str, Any] | None,
    selector: str,
    agent_role: str | None = None,
    agent_name: str | None = None,
    platform_role_routing: dict[str, str] | None = None,
    platform_name_routing: dict[str, str] | None = None,
) -> AgentExecutionProfile:
    profile, _profiles = _resolve_agent_execution_profiles(
        settings=settings,
        tenant_policy=tenant_policy,
        project_overrides=project_overrides,
        selector=selector,
        agent_role=agent_role,
        agent_name=agent_name,
        platform_role_routing=platform_role_routing,
        platform_name_routing=platform_name_routing,
    )
    return profile


def _platform_agent_runtime_routing(*, session) -> tuple[dict[str, str], dict[str, str]]:  # noqa: ANN001
    if session is None:
        return {}, {}
    payload = platform_settings_service.get_json(
        session=session,
        setting_key=SETTING_KEY_AGENT_RUNTIME_ROUTING,
    )
    return (
        normalize_agent_routing(payload.get("role_routing")),
        normalize_agent_routing(payload.get("name_routing")),
    )


def build_runtime_for_selector(
    *,
    session,
    settings: Settings,
    tenant_id: str | None,
    project_id: str | None,
    selector: str,
    agent_role: str | None = None,
    agent_name: str | None = None,
):  # noqa: ANN001
    tenant_policy: dict[str, Any] = {}
    project_overrides: dict[str, Any] = {}
    normalized_tenant_id = str(tenant_id or "").strip()
    normalized_project_id = str(project_id or "").strip()
    if session is not None and normalized_tenant_id:
        tenant = session.get(Tenant, normalized_tenant_id)
        if tenant is not None:
            tenant_policy = dict(tenant.policy_config or {})
    if session is not None and normalized_project_id:
        project = session.get(Project, normalized_project_id)
        if project is not None and (not normalized_tenant_id or project.tenant_id == normalized_tenant_id):
            project_overrides = dict(project.policy_overrides or {})
    platform_role_routing, platform_name_routing = _platform_agent_runtime_routing(session=session)
    profile, profiles = _resolve_agent_execution_profiles(
        settings=settings,
        tenant_policy=tenant_policy,
        project_overrides=project_overrides,
        selector=selector,
        agent_role=agent_role,
        agent_name=agent_name,
        platform_role_routing=platform_role_routing,
        platform_name_routing=platform_name_routing,
    )
    runtime = build_runtime_for_execution_profile(
        session=session,
        settings=settings,
        profile=profile,
    )
    fallback_name = str(profile.fallback_profile or "").strip()
    if not fallback_name or fallback_name == profile.profile_name:
        return runtime
    fallback_runtime = build_runtime_for_execution_profile(
        session=session,
        settings=settings,
        profile=build_agent_execution_profile(
            profile_name=fallback_name,
            profiles=profiles,
        ),
    )
    return build_runtime_with_fallback(
        primary_runtime=runtime,
        fallback_runtime=fallback_runtime,
    )
