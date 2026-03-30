from __future__ import annotations

from dataclasses import dataclass
from typing import Any

VOICE_ROOM_PERSONA_IDS = ("pm", "architect", "engineer", "qa", "security")
DEFAULT_VOICE_ROOM_PERSONA_ID = "pm"
_VOICE_ROOM_ROLE_LABELS = {
    "pm": "Product",
    "architect": "Architecture",
    "engineer": "Engineering",
    "qa": "QA",
    "security": "Security",
    "reviewer": "Review",
}
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
    default_voice_id: str
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
        default_display_name="Andy",
        default_voice_id="marius",
        system_prompt_template="discord/voice_room_pm_system.j2",
        user_prompt_template="discord/voice_room_pm_user.j2",
    ),
    "architect": VoiceRoomPersonaDefinition(
        persona_id="architect",
        role_label="Architect",
        default_display_name="Soren",
        default_voice_id="javert",
        system_prompt_template="discord/voice_room_architect_system.j2",
        user_prompt_template="discord/voice_room_architect_user.j2",
    ),
    "engineer": VoiceRoomPersonaDefinition(
        persona_id="engineer",
        role_label="Engineer",
        default_display_name="Judy",
        default_voice_id="eponine",
        system_prompt_template="discord/voice_room_engineer_system.j2",
        user_prompt_template="discord/voice_room_engineer_user.j2",
    ),
    "qa": VoiceRoomPersonaDefinition(
        persona_id="qa",
        role_label="QA",
        default_display_name="Quinn",
        default_voice_id="cosette",
        system_prompt_template="discord/voice_room_qa_system.j2",
        user_prompt_template="discord/voice_room_qa_user.j2",
    ),
    "security": VoiceRoomPersonaDefinition(
        persona_id="security",
        role_label="Security",
        default_display_name="June",
        default_voice_id="fantine",
        system_prompt_template="discord/voice_room_security_system.j2",
        user_prompt_template="discord/voice_room_security_user.j2",
    ),
    "reviewer": VoiceRoomPersonaDefinition(
        persona_id="reviewer",
        role_label="Reviewer",
        default_display_name="Mira",
        default_voice_id="azelma",
        system_prompt_template="discord/voice_room_reviewer_system.j2",
        user_prompt_template="discord/voice_room_reviewer_user.j2",
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
    for persona_id in VOICE_ROOM_PERSONA_IDS:
        definition = VOICE_ROOM_PERSONAS[persona_id]
        personas.append(
            {
                "persona_id": definition.persona_id,
                "role_label": definition.role_label,
                "display_name": definition.default_display_name,
                "voice_id": definition.default_voice_id,
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
    if voice_id is None:
        voice_id = definition.default_voice_id

    return VoiceRoomPersonaProfile(
        persona_id=definition.persona_id,
        role_label=definition.role_label,
        display_name=display_name,
        voice_id=voice_id,
    )


def resolve_voice_room_persona_audience_role(
    *,
    persona_id: object | None = None,
    persona_role: object | None = None,
) -> str:
    normalized_persona_id = str(persona_id or "").strip().lower()
    if normalized_persona_id in _VOICE_ROOM_ROLE_LABELS:
        return _VOICE_ROOM_ROLE_LABELS[normalized_persona_id]

    normalized_role = str(persona_role or "").strip()
    if not normalized_role:
        return ""
    role_key = normalized_role.lower()
    if role_key in _VOICE_ROOM_ROLE_LABELS:
        return _VOICE_ROOM_ROLE_LABELS[role_key]
    if role_key == "product":
        return "Product"
    if role_key == "architecture":
        return "Architecture"
    if role_key == "engineering":
        return "Engineering"
    if role_key == "qa":
        return "QA"
    if role_key == "security":
        return "Security"
    return normalized_role


def format_voice_room_persona_label(
    *,
    persona_id: object | None = None,
    persona_name: object | None = None,
    persona_role: object | None = None,
) -> str:
    normalized_persona_id = str(persona_id or "").strip().lower()
    definition = VOICE_ROOM_PERSONAS.get(normalized_persona_id)
    display_name = str(persona_name or "").strip()
    audience_role = resolve_voice_room_persona_audience_role(
        persona_id=persona_id,
        persona_role=persona_role,
    )

    if _is_generic_persona_label(
        display_name,
        persona_id=normalized_persona_id,
        persona_role=persona_role,
        audience_role=audience_role,
    ):
        display_name = definition.default_display_name if definition is not None else ""

    if not display_name:
        return audience_role or "Room persona"

    if audience_role and display_name.casefold() != audience_role.casefold():
        return f"{display_name} from {audience_role}"
    return display_name


def build_voice_room_spoken_reply_text(
    *,
    message: object,
    persona_id: object | None = None,
    persona_name: object | None = None,
    persona_role: object | None = None,
) -> str:
    normalized_message = str(message or "").strip()
    persona_label = format_voice_room_persona_label(
        persona_id=persona_id,
        persona_name=persona_name,
        persona_role=persona_role,
    )
    if not persona_label:
        return normalized_message
    if not normalized_message:
        return persona_label
    return f"{persona_label}. {normalized_message}"


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


def _is_generic_persona_label(
    value: str,
    *,
    persona_id: str,
    persona_role: object | None,
    audience_role: str,
) -> bool:
    normalized_value = value.strip().casefold()
    if not normalized_value:
        return True

    generic_values = {
        persona_id.casefold(),
        str(persona_role or "").strip().casefold(),
        audience_role.strip().casefold(),
    }
    generic_values.discard("")
    return normalized_value in generic_values
