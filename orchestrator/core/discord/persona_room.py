from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class VoiceRoomTurnResult:
    persona_id: str
    persona_role: str
    persona_name: str
    persona_voice_id: str | None
    message: str
    brief: dict[str, Any]
    router_confidence: float
    router_reason: str
    room_config: dict[str, dict[str, str]]
