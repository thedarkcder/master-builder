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

- Router and Discord answer paths can use `invoke_codex_json_with_tools` when a SQLAlchemy session and settings are provided; allowlists live in `TOOL_ALLOWLIST` (`voice_entry_router`, `discord_ask_answer`, `discord_voice_room_persona`, `discord_pm_interview`).
- Voice `!ask` merged response data sets `room_mode` and `room_source` (`voice_note`, `live_voice`) so `build_command_followup_message` prefixes persona labels consistently with TTS overlay.

## Future: unified post-router service (optional)

A single **voice orchestration** module could:

- Own STT handoff, router invocation, and dispatch to PM vs ask vs persona handlers.
- Emit a stable contract, e.g. `{ channel_message_markdown, spoken_text, persona_id, tts_metadata, history_updates }`.
- Centralize telemetry and hop limits across stages.

That refactor is **optional**; current command-based wiring remains valid.
