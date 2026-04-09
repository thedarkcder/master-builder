from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from orchestrator.core.codex_models import (
    CodexModelOption,
    normalize_codex_model,
    normalize_codex_reasoning_effort,
    parse_supported_codex_models,
)

PROFILE_PM_CONVERSATION = "pm_conversation"
PROFILE_ENGINEERING_EXECUTION = "engineering_execution"
PROFILE_GENERAL_PLANNING = "general_planning"
PROFILE_MARKETING_CONVERSATION = "marketing_conversation"
PROFILE_PM_CONVERSATION_DEFAULT = "pm_conversation_default"
PROFILE_PM_CONVERSATION_FAST = "pm_conversation_fast"
PROFILE_ENGINEERING_EXECUTION_DEFAULT = "engineering_execution_default"
PROFILE_ENGINEERING_EXECUTION_FAST = "engineering_execution_fast"
PROFILE_ENGINEERING_EXECUTION_DEEP = "engineering_execution_deep"
PROFILE_GENERAL_PLANNING_DEFAULT = "general_planning_default"
PROFILE_MARKETING_CONVERSATION_DEFAULT = "marketing_conversation_default"

AGENT_ROLE_PM = "pm"
AGENT_ROLE_ENGINEERING = "engineering"
AGENT_ROLE_TEST = "test"
AGENT_ROLE_REVIEW = "review"
AGENT_ROLE_MARKETING = "marketing"

AGENT_NAME_PM_PRIMARY = "pm_primary"
AGENT_NAME_VOICE_ROOM_PM = "voice_room_pm"
AGENT_NAME_WORKFLOW_DEV_DEFAULT = "workflow_dev_default"
AGENT_NAME_WORKFLOW_TEST_DEFAULT = "workflow_test_default"
AGENT_NAME_WORKFLOW_REVIEW_DEFAULT = "workflow_review_default"
AGENT_NAME_MARKETING_DEFAULT = "marketing_default"

RUNTIME_KIND_CODEX_CLI = "codex_cli"
RUNTIME_KIND_CHAT_CLI = "chat_cli"
RUNTIME_KIND_CLAUDE_CLI = "claude_cli"
RUNTIME_KIND_OPENAI = "openai"
RUNTIME_KIND_CLAUDE = "claude"
RUNTIME_KIND_LLAMA_CPP = "llama_cpp"
RUNTIME_KIND_LM_STUDIO = "lm_studio"

_SUPPORTED_RUNTIME_KINDS = {
    RUNTIME_KIND_CODEX_CLI,
    RUNTIME_KIND_CHAT_CLI,
    RUNTIME_KIND_CLAUDE_CLI,
    RUNTIME_KIND_OPENAI,
    RUNTIME_KIND_CLAUDE,
    RUNTIME_KIND_LLAMA_CPP,
    RUNTIME_KIND_LM_STUDIO,
}
_CLI_RUNTIME_KINDS = {
    RUNTIME_KIND_CODEX_CLI,
    RUNTIME_KIND_CHAT_CLI,
    RUNTIME_KIND_CLAUDE_CLI,
}
_HTTP_RUNTIME_KINDS = {
    RUNTIME_KIND_OPENAI,
    RUNTIME_KIND_CLAUDE,
    RUNTIME_KIND_LLAMA_CPP,
    RUNTIME_KIND_LM_STUDIO,
}
_BASE_URL_REQUIRED_RUNTIME_KINDS = {
    RUNTIME_KIND_LLAMA_CPP,
    RUNTIME_KIND_LM_STUDIO,
}
_REASONING_SUPPORTED_RUNTIME_KINDS = {
    RUNTIME_KIND_CODEX_CLI,
    RUNTIME_KIND_CHAT_CLI,
    RUNTIME_KIND_CLAUDE_CLI,
    RUNTIME_KIND_OPENAI,
    RUNTIME_KIND_CLAUDE,
    RUNTIME_KIND_LLAMA_CPP,
    RUNTIME_KIND_LM_STUDIO,
}
_DEFAULT_ROUTING = {
    "discord.pm_answer": PROFILE_PM_CONVERSATION,
    "discord.voice_room_pm": PROFILE_PM_CONVERSATION,
    "discord.ask_answer": PROFILE_GENERAL_PLANNING,
    "discord.ask_intent": PROFILE_GENERAL_PLANNING,
    "discord.issue_seed": PROFILE_GENERAL_PLANNING,
    "discord.voice_entry_router": PROFILE_GENERAL_PLANNING,
    "discord.voice_room_architect": PROFILE_GENERAL_PLANNING,
    "discord.voice_room_engineer": PROFILE_GENERAL_PLANNING,
    "discord.voice_room_qa": PROFILE_GENERAL_PLANNING,
    "discord.voice_room_security": PROFILE_GENERAL_PLANNING,
    "workflow.pm": PROFILE_GENERAL_PLANNING,
    "workflow.dev": PROFILE_ENGINEERING_EXECUTION,
    "workflow.test": PROFILE_ENGINEERING_EXECUTION,
    "workflow.review": PROFILE_ENGINEERING_EXECUTION,
    "workflow.standup_voice_brief": PROFILE_GENERAL_PLANNING,
    "workflow.retro_voice_brief": PROFILE_GENERAL_PLANNING,
    "workflow.design_planning": PROFILE_GENERAL_PLANNING,
    "workflow.design_review": PROFILE_GENERAL_PLANNING,
    "workflow.design_feedback": PROFILE_GENERAL_PLANNING,
}
_DEFAULT_AGENT_ROLE_ROUTING = {
    AGENT_ROLE_PM: PROFILE_PM_CONVERSATION_DEFAULT,
    AGENT_ROLE_ENGINEERING: PROFILE_ENGINEERING_EXECUTION_DEFAULT,
    AGENT_ROLE_TEST: PROFILE_ENGINEERING_EXECUTION_DEFAULT,
    AGENT_ROLE_REVIEW: PROFILE_ENGINEERING_EXECUTION_DEFAULT,
    AGENT_ROLE_MARKETING: PROFILE_MARKETING_CONVERSATION_DEFAULT,
}
_DEFAULT_AGENT_NAME_ROUTING = {
    AGENT_NAME_PM_PRIMARY: PROFILE_PM_CONVERSATION_DEFAULT,
    AGENT_NAME_VOICE_ROOM_PM: PROFILE_PM_CONVERSATION_FAST,
    AGENT_NAME_WORKFLOW_DEV_DEFAULT: PROFILE_ENGINEERING_EXECUTION_DEFAULT,
    AGENT_NAME_WORKFLOW_TEST_DEFAULT: PROFILE_ENGINEERING_EXECUTION_FAST,
    AGENT_NAME_WORKFLOW_REVIEW_DEFAULT: PROFILE_ENGINEERING_EXECUTION_DEEP,
    AGENT_NAME_MARKETING_DEFAULT: PROFILE_MARKETING_CONVERSATION_DEFAULT,
}
_RUNTIME_LABELS = {
    RUNTIME_KIND_CODEX_CLI: "Codex CLI",
    RUNTIME_KIND_CHAT_CLI: "Chat CLI",
    RUNTIME_KIND_CLAUDE_CLI: "Claude CLI",
    RUNTIME_KIND_OPENAI: "OpenAI API",
    RUNTIME_KIND_CLAUDE: "Claude API",
    RUNTIME_KIND_LLAMA_CPP: "llama.cpp",
    RUNTIME_KIND_LM_STUDIO: "LM Studio",
}
_STATIC_MODEL_OPTIONS: dict[str, tuple[tuple[str, str, str | None], ...]] = {
    RUNTIME_KIND_OPENAI: (
        ("gpt-5.4", "GPT-5.4", "General-purpose OpenAI model"),
        ("gpt-4.1", "GPT-4.1", "Broad compatibility OpenAI model"),
    ),
    RUNTIME_KIND_CLAUDE: (
        ("claude-sonnet-4-0", "Claude Sonnet 4", "Balanced Anthropic model"),
        ("claude-opus-4-0", "Claude Opus 4", "Deep reasoning Anthropic model"),
        ("claude-3-5-haiku-latest", "Claude Haiku", "Fast Anthropic model"),
    ),
    RUNTIME_KIND_CLAUDE_CLI: (
        ("claude-sonnet-4-0", "Claude Sonnet 4", "Balanced Claude CLI model"),
        ("claude-opus-4-0", "Claude Opus 4", "Deep Claude CLI model"),
        ("claude-3-5-haiku-latest", "Claude Haiku", "Fast Claude CLI model"),
    ),
    RUNTIME_KIND_LLAMA_CPP: (),
    RUNTIME_KIND_LM_STUDIO: (),
}


@dataclass(frozen=True)
class AgentExecutionProfile:
    profile_name: str
    runtime_kind: str
    cli_command: str
    model: str
    reasoning_effort: str | None
    tool_bridge_allowed: bool
    fallback_profile: str | None = None
    base_url: str | None = None
    api_key_secret_ref: str | None = None


def list_supported_runtime_kinds() -> list[str]:
    return sorted(_SUPPORTED_RUNTIME_KINDS)


def runtime_kind_label(runtime_kind: str) -> str:
    normalized = str(runtime_kind or "").strip().lower()
    return _RUNTIME_LABELS.get(normalized, normalized or "Unknown")


def runtime_kind_requires_base_url(runtime_kind: str) -> bool:
    return str(runtime_kind or "").strip().lower() in _BASE_URL_REQUIRED_RUNTIME_KINDS


def runtime_kind_supports_reasoning_effort(runtime_kind: str) -> bool:
    return str(runtime_kind or "").strip().lower() in _REASONING_SUPPORTED_RUNTIME_KINDS


def runtime_kind_supports_api_key(runtime_kind: str) -> bool:
    normalized = str(runtime_kind or "").strip().lower()
    return normalized in _HTTP_RUNTIME_KINDS


def default_execution_profile_routing() -> dict[str, str]:
    return dict(_DEFAULT_ROUTING)


def default_agent_role_routing() -> dict[str, str]:
    return dict(_DEFAULT_AGENT_ROLE_ROUTING)


def default_agent_name_routing() -> dict[str, str]:
    return dict(_DEFAULT_AGENT_NAME_ROUTING)


def normalize_agent_routing(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, str] = {}
    for raw_key, raw_profile_name in raw.items():
        key = str(raw_key or "").strip()
        profile_name = str(raw_profile_name or "").strip()
        if key and profile_name:
            normalized[key] = profile_name
    return normalized


def merge_agent_routing(
    *,
    default_routing: dict[str, str],
    configured_routing: dict[str, str] | None,
) -> dict[str, str]:
    merged = dict(default_routing)
    merged.update(configured_routing or {})
    return merged


def _select_fast_codex_model(*, default_model: str, configured_models: str | None) -> str:
    options = parse_supported_codex_models(default_model=default_model, configured_models=configured_models)
    for candidate in options:
        if candidate.model_id == "gpt-5.3-codex-spark":
            return candidate.model_id
    if len(options) >= 2:
        return options[1].model_id
    return normalize_codex_model(default_model) or "gpt-5.4"


def default_execution_profiles(
    *,
    default_codex_cli_command: str,
    default_codex_model: str,
    default_codex_reasoning_effort: str,
    default_codex_supported_models: str | None = None,
    default_chat_cli_command: str | None = None,
    default_chat_model: str | None = None,
    default_chat_reasoning_effort: str | None = None,
    default_claude_cli_command: str | None = None,
) -> dict[str, dict[str, Any]]:
    normalized_codex_command = str(default_codex_cli_command or "").strip() or "codex"
    normalized_chat_command = str(default_chat_cli_command or "").strip() or normalized_codex_command
    normalized_codex_model = normalize_codex_model(default_codex_model) or "gpt-5.4"
    normalized_chat_model = normalize_codex_model(default_chat_model) or normalized_codex_model
    normalized_codex_effort = normalize_codex_reasoning_effort(default_codex_reasoning_effort) or "medium"
    normalized_chat_effort = normalize_codex_reasoning_effort(default_chat_reasoning_effort) or normalized_codex_effort
    fast_codex_model = _select_fast_codex_model(
        default_model=normalized_codex_model,
        configured_models=default_codex_supported_models,
    )
    return {
        PROFILE_PM_CONVERSATION: {
            "runtime_kind": RUNTIME_KIND_CHAT_CLI,
            "cli_command": normalized_chat_command,
            "model": normalized_chat_model,
            "reasoning_effort": normalized_chat_effort,
            "tool_bridge_allowed": False,
            "fallback_profile": PROFILE_GENERAL_PLANNING,
        },
        PROFILE_PM_CONVERSATION_DEFAULT: {
            "runtime_kind": RUNTIME_KIND_CHAT_CLI,
            "cli_command": normalized_chat_command,
            "model": normalized_chat_model,
            "reasoning_effort": normalized_chat_effort,
            "tool_bridge_allowed": False,
            "fallback_profile": PROFILE_GENERAL_PLANNING_DEFAULT,
        },
        PROFILE_PM_CONVERSATION_FAST: {
            "runtime_kind": RUNTIME_KIND_CHAT_CLI,
            "cli_command": normalized_chat_command,
            "model": normalized_chat_model,
            "reasoning_effort": "low",
            "tool_bridge_allowed": False,
            "fallback_profile": PROFILE_GENERAL_PLANNING_DEFAULT,
        },
        PROFILE_ENGINEERING_EXECUTION: {
            "runtime_kind": RUNTIME_KIND_CODEX_CLI,
            "cli_command": normalized_codex_command,
            "model": normalized_codex_model,
            "reasoning_effort": normalized_codex_effort,
            "tool_bridge_allowed": True,
        },
        PROFILE_ENGINEERING_EXECUTION_DEFAULT: {
            "runtime_kind": RUNTIME_KIND_CODEX_CLI,
            "cli_command": normalized_codex_command,
            "model": normalized_codex_model,
            "reasoning_effort": normalized_codex_effort,
            "tool_bridge_allowed": True,
        },
        PROFILE_ENGINEERING_EXECUTION_FAST: {
            "runtime_kind": RUNTIME_KIND_CODEX_CLI,
            "cli_command": normalized_codex_command,
            "model": fast_codex_model,
            "reasoning_effort": "low",
            "tool_bridge_allowed": True,
        },
        PROFILE_ENGINEERING_EXECUTION_DEEP: {
            "runtime_kind": RUNTIME_KIND_CODEX_CLI,
            "cli_command": normalized_codex_command,
            "model": normalized_codex_model,
            "reasoning_effort": "high",
            "tool_bridge_allowed": True,
        },
        PROFILE_GENERAL_PLANNING: {
            "runtime_kind": RUNTIME_KIND_CODEX_CLI,
            "cli_command": normalized_codex_command,
            "model": normalized_codex_model,
            "reasoning_effort": normalized_codex_effort,
            "tool_bridge_allowed": True,
        },
        PROFILE_GENERAL_PLANNING_DEFAULT: {
            "runtime_kind": RUNTIME_KIND_CODEX_CLI,
            "cli_command": normalized_codex_command,
            "model": normalized_codex_model,
            "reasoning_effort": normalized_codex_effort,
            "tool_bridge_allowed": True,
        },
        PROFILE_MARKETING_CONVERSATION: {
            "runtime_kind": RUNTIME_KIND_CHAT_CLI,
            "cli_command": normalized_chat_command,
            "model": normalized_chat_model,
            "reasoning_effort": normalized_chat_effort,
            "tool_bridge_allowed": False,
            "fallback_profile": PROFILE_GENERAL_PLANNING,
        },
        PROFILE_MARKETING_CONVERSATION_DEFAULT: {
            "runtime_kind": RUNTIME_KIND_CHAT_CLI,
            "cli_command": normalized_chat_command,
            "model": normalized_chat_model,
            "reasoning_effort": normalized_chat_effort,
            "tool_bridge_allowed": False,
            "fallback_profile": PROFILE_GENERAL_PLANNING_DEFAULT,
        },
    }


def _normalized_runtime_kind(value: Any) -> str | None:
    normalized = str(value or "").strip().lower()
    if normalized in _SUPPORTED_RUNTIME_KINDS:
        return normalized
    return None


def _normalize_profile_record(raw_profile: dict[str, Any]) -> dict[str, Any] | None:
    runtime_kind = _normalized_runtime_kind(raw_profile.get("runtime_kind"))
    if runtime_kind is None:
        return None
    model = normalize_codex_model(raw_profile.get("model"))
    if model is None:
        return None

    cli_command = str(raw_profile.get("cli_command") or "").strip()
    base_url = str(raw_profile.get("base_url") or "").strip()
    api_key_secret_ref = str(raw_profile.get("api_key_secret_ref") or "").strip()

    if runtime_kind in _CLI_RUNTIME_KINDS and not cli_command:
        return None
    if runtime_kind in _BASE_URL_REQUIRED_RUNTIME_KINDS and not base_url:
        return None

    profile: dict[str, Any] = {
        "runtime_kind": runtime_kind,
        "cli_command": cli_command,
        "model": model,
        "tool_bridge_allowed": bool(raw_profile.get("tool_bridge_allowed")),
    }
    reasoning_effort = normalize_codex_reasoning_effort(raw_profile.get("reasoning_effort"))
    if reasoning_effort is not None and runtime_kind_supports_reasoning_effort(runtime_kind):
        profile["reasoning_effort"] = reasoning_effort
    fallback_profile = str(raw_profile.get("fallback_profile") or "").strip()
    if fallback_profile:
        profile["fallback_profile"] = fallback_profile
    if base_url:
        profile["base_url"] = base_url
    if api_key_secret_ref:
        profile["api_key_secret_ref"] = api_key_secret_ref
    return profile


def normalize_execution_profiles(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, dict[str, Any]] = {}
    for raw_name, raw_profile in raw.items():
        profile_name = str(raw_name or "").strip()
        if not profile_name or not isinstance(raw_profile, dict):
            continue
        profile = _normalize_profile_record(raw_profile)
        if profile is None:
            continue
        normalized[profile_name] = profile
    return normalized


def normalize_execution_profile_routing(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, str] = {}
    for raw_selector, raw_profile_name in raw.items():
        selector = str(raw_selector or "").strip()
        profile_name = str(raw_profile_name or "").strip()
        if selector and profile_name:
            normalized[selector] = profile_name
    return normalized


def merge_execution_profiles(
    *,
    default_profiles: dict[str, dict[str, Any]],
    platform_profiles: dict[str, dict[str, Any]] | None = None,
    tenant_profiles: dict[str, dict[str, Any]] | None = None,
    project_profiles: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    merged = deepcopy(default_profiles)
    for source in (platform_profiles or {}, tenant_profiles or {}, project_profiles or {}):
        for profile_name, profile in source.items():
            merged[profile_name] = deepcopy(profile)
    return merged


def merge_execution_profile_routing(
    *,
    tenant_routing: dict[str, str] | None,
    project_routing: dict[str, str] | None,
) -> dict[str, str]:
    merged = dict(_DEFAULT_ROUTING)
    merged.update(tenant_routing or {})
    merged.update(project_routing or {})
    return merged


def resolve_execution_profile_name(
    *,
    routing: dict[str, str],
    selector: str,
    agent_role: str | None = None,
    agent_name: str | None = None,
    platform_role_routing: dict[str, str] | None = None,
    platform_name_routing: dict[str, str] | None = None,
) -> str:
    normalized_agent_name = str(agent_name or "").strip()
    normalized_agent_role = str(agent_role or "").strip()
    if normalized_agent_name:
        profile_name = str((platform_name_routing or {}).get(normalized_agent_name) or "").strip()
        if profile_name:
            return profile_name
    if normalized_agent_role:
        profile_name = str((platform_role_routing or {}).get(normalized_agent_role) or "").strip()
        if profile_name:
            return profile_name
    normalized_selector = str(selector or "").strip()
    if not normalized_selector:
        return PROFILE_GENERAL_PLANNING
    return str(routing.get(normalized_selector) or PROFILE_GENERAL_PLANNING).strip() or PROFILE_GENERAL_PLANNING


def build_agent_execution_profile(
    *,
    profile_name: str,
    profiles: dict[str, dict[str, Any]],
) -> AgentExecutionProfile:
    raw_profile = dict(profiles.get(profile_name) or profiles.get(PROFILE_GENERAL_PLANNING) or {})
    runtime_kind = _normalized_runtime_kind(raw_profile.get("runtime_kind")) or RUNTIME_KIND_CODEX_CLI
    cli_command = str(raw_profile.get("cli_command") or "").strip()
    model = normalize_codex_model(raw_profile.get("model")) or "gpt-5.4"
    reasoning_effort = normalize_codex_reasoning_effort(raw_profile.get("reasoning_effort"))
    fallback_profile = str(raw_profile.get("fallback_profile") or "").strip() or None
    base_url = str(raw_profile.get("base_url") or "").strip() or None
    api_key_secret_ref = str(raw_profile.get("api_key_secret_ref") or "").strip() or None
    return AgentExecutionProfile(
        profile_name=profile_name,
        runtime_kind=runtime_kind,
        cli_command=cli_command,
        model=model,
        reasoning_effort=reasoning_effort if runtime_kind_supports_reasoning_effort(runtime_kind) else None,
        tool_bridge_allowed=bool(raw_profile.get("tool_bridge_allowed")),
        fallback_profile=fallback_profile,
        base_url=base_url,
        api_key_secret_ref=api_key_secret_ref,
    )


def collect_models_for_runtime_kind(
    *,
    runtime_kind: str,
    default_model: str,
    codex_supported_models: str | None,
    profiles: dict[str, dict[str, Any]] | None = None,
) -> list[CodexModelOption]:
    normalized_runtime_kind = _normalized_runtime_kind(runtime_kind) or RUNTIME_KIND_CODEX_CLI
    options: list[CodexModelOption] = []
    seen: set[str] = set()

    def add_option(model_id: str, label: str | None = None, description: str | None = None) -> None:
        normalized_model = normalize_codex_model(model_id)
        if normalized_model is None or normalized_model in seen:
            return
        seen.add(normalized_model)
        options.append(
            CodexModelOption(
                model_id=normalized_model,
                label=label or normalized_model,
                description=description,
            )
        )

    if normalized_runtime_kind in {RUNTIME_KIND_CODEX_CLI, RUNTIME_KIND_CHAT_CLI}:
        for option in parse_supported_codex_models(
            default_model=default_model,
            configured_models=codex_supported_models,
        ):
            add_option(option.model_id, option.label, option.description)

    for model_id, label, description in _STATIC_MODEL_OPTIONS.get(normalized_runtime_kind, ()):
        add_option(model_id, label, description)

    for profile in (profiles or {}).values():
        if _normalized_runtime_kind(profile.get("runtime_kind")) != normalized_runtime_kind:
            continue
        add_option(str(profile.get("model") or ""))

    if not options:
        add_option(default_model)
    return options
