# Voice orchestration: target architecture (design note)

This document captures the **intended** end-state for Discord voice (STT → route → answer → TTS) discussed in product planning. It is not a commitment to a single mega-PR; it guides incremental work.

## Target pipeline

1. **STT** produces a transcript (voice note in channel, live voice room, etc.).
2. **Router** classifies into lanes (**ask** vs **interview**); **ask** carries a **persona** for answer prompts and TTS. The router may **call tools** when routing would be wrong without fresh Jira/knowledge/repo facts.
3. **Answer runtime** (per lane/persona) produces conversational text and may **call tools** under a **stage-specific allowlist** and hop budget.
4. **Discord + TTS** consume one coherent payload: user mention, persona label in **text** when in voice/room mode, and matching TTS metadata.

## Branches

- **Q&A** — Short board-style answers (`!ask`-class behavior).
- **PM / product interview** — Multi-turn `!pm` interview state, seeding, planning when applicable.

## Implementation status (snapshot)

- Router and Discord answer paths can use `invoke_runtime_json_with_tools` when a SQLAlchemy session and settings are provided; allowlists live in `TOOL_ALLOWLIST` (`voice_entry_router`, `discord_ask_answer`, `discord_voice_room_persona`, `discord_pm_interview`).
- Voice `!ask` merged response data sets `room_mode` and `room_source` (`voice_note`, `live_voice`) so `build_command_followup_message` prefixes persona labels consistently with TTS overlay.

## Future: unified post-router service (optional)

A single **voice orchestration** module could:

- Own STT handoff, router invocation, and dispatch to `!ask` vs `!pm` only (persona remains ask-answer framing, not a separate command path).
- Emit a stable contract, e.g. `{ channel_message_markdown, spoken_text, persona_id, tts_metadata, history_updates }`.
- Centralize telemetry and hop limits across stages.

That refactor is **optional**; current command-based wiring remains valid.

## Voice note vs live voice (transport and delivery)

Routing is the same for both: transcript → `route_discord_voice_entry` → `!ask` / `!pm` with shared parameters. Only ingestion and playback differ.

| Aspect | Voice note (gateway) | Live voice |
| --- | --- | --- |
| Ingress | `build_discord_message_ingress_result` → `execute_tenant_command_ingress` | `DiscordLiveVoiceService` → same executor after STT |
| Success UX | Channel message; when room voice reply is enabled, **one** post with text + optional `.wav` attachment | TTS → voice-channel playback; linked text channel usually gets notices on failure only (no duplicate full-text mirror unless product adds an optional flag later) |
| Default persona | `pm` when router/payload does not specify | Same |

Voice notes avoid posting a separate full-text message when the attachment message already carries the same body (`content_override`).
