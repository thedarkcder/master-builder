from __future__ import annotations

from dataclasses import dataclass
from typing import Any

VOICE_ROOM_PERSONA_IDS = ("pm", "architect", "engineer", "qa", "security")
DEFAULT_VOICE_ROOM_PERSONA_ID = "pm"
_PERSONA_NAME_KEYS = (
    "persona_names",
    "voice_room_persona_names",
    "room_persona_names",
    "pm_room_persona_names",
)
_PERSONA_VOICE_KEYS = (
    "persona_voices",
    "voice_room_persona_voices",
    "room_persona_voices",
    "pm_room_persona_voices",
)


@dataclass(frozen=True)
class VoiceRoomPersonaDefinition:
    persona_id: str
    role_label: str
    default_display_name: str
    system_prompt_template: str
    user_prompt_template: str


@dataclass(frozen=True)
class VoiceRoomPersonaProfile:
    persona_id: str
    role_label: str
    display_name: str
    voice_id: str | None


VOICE_ROOM_PERSONAS: dict[str, VoiceRoomPersonaDefinition] = {
    "pm": VoiceRoomPersonaDefinition(
        persona_id="pm",
        role_label="PM",
        default_display_name="PM",
        system_prompt_template="discord/voice_room_pm_system.j2",
        user_prompt_template="discord/voice_room_pm_user.j2",
    ),
    "architect": VoiceRoomPersonaDefinition(
        persona_id="architect",
        role_label="Architect",
        default_display_name="Architect",
        system_prompt_template="discord/voice_room_architect_system.j2",
        user_prompt_template="discord/voice_room_architect_user.j2",
    ),
    "engineer": VoiceRoomPersonaDefinition(
        persona_id="engineer",
        role_label="Engineer",
        default_display_name="Engineer",
        system_prompt_template="discord/voice_room_engineer_system.j2",
        user_prompt_template="discord/voice_room_engineer_user.j2",
    ),
    "qa": VoiceRoomPersonaDefinition(
        persona_id="qa",
        role_label="QA",
        default_display_name="QA",
        system_prompt_template="discord/voice_room_qa_system.j2",
        user_prompt_template="discord/voice_room_qa_user.j2",
    ),
    "security": VoiceRoomPersonaDefinition(
        persona_id="security",
        role_label="Security",
        default_display_name="Security",
        system_prompt_template="discord/voice_room_security_system.j2",
        user_prompt_template="discord/voice_room_security_user.j2",
    ),
}


def normalize_voice_room_persona_id(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in VOICE_ROOM_PERSONAS:
        return normalized
    return DEFAULT_VOICE_ROOM_PERSONA_ID


def get_voice_room_persona_definition(persona_id: object) -> VoiceRoomPersonaDefinition:
    normalized = normalize_voice_room_persona_id(persona_id)
    return VOICE_ROOM_PERSONAS[normalized]


def list_voice_room_personas() -> list[dict[str, str]]:
    personas: list[dict[str, str]] = []
    for definition in VOICE_ROOM_PERSONAS.values():
        personas.append(
            {
                "persona_id": definition.persona_id,
                "role_label": definition.role_label,
                "display_name": definition.default_display_name,
            }
        )
    return personas


def build_voice_room_config(*discord_configs: dict | None) -> dict[str, dict[str, str]]:
    persona_names: dict[str, str] = {}
    persona_voices: dict[str, str] = {}
    for discord_config in discord_configs:
        if not isinstance(discord_config, dict):
            continue
        persona_names.update(_merged_persona_map(discord_config, _PERSONA_NAME_KEYS))
        persona_voices.update(_merged_persona_map(discord_config, _PERSONA_VOICE_KEYS))
    normalized: dict[str, dict[str, str]] = {}
    if persona_names:
        normalized["persona_names"] = persona_names
    if persona_voices:
        normalized["persona_voices"] = persona_voices
    return normalized


def resolve_voice_room_persona_profile(
    *,
    persona_id: object,
    tenant_discord_config: dict | None = None,
    project_discord_config: dict | None = None,
) -> VoiceRoomPersonaProfile:
    definition = get_voice_room_persona_definition(persona_id)
    room_config = build_voice_room_config(tenant_discord_config, project_discord_config)
    persona_names = room_config.get("persona_names") or {}
    persona_voices = room_config.get("persona_voices") or {}

    display_name = (
        str(persona_names.get(definition.persona_id) or "").strip()
        or str(persona_names.get("default") or "").strip()
        or definition.default_display_name
    )
    voice_id = str(persona_voices.get(definition.persona_id) or "").strip() or None
    if voice_id is None:
        voice_id = str(persona_voices.get("default") or "").strip() or None

    return VoiceRoomPersonaProfile(
        persona_id=definition.persona_id,
        role_label=definition.role_label,
        display_name=display_name,
        voice_id=voice_id,
    )


def _merged_persona_map(discord_config: dict[str, Any], keys: tuple[str, ...]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for key in keys:
        merged.update(_normalize_string_map(discord_config.get(key)))
    return merged


def _normalize_string_map(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, str] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key or "").strip().lower()
        normalized_value = str(raw_value or "").strip()
        if not key or not normalized_value:
            continue
        normalized[key] = normalized_value
    return normalized
