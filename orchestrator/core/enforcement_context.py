from __future__ import annotations

import json
from pathlib import Path


GTD_RULES = """Good To Do checklist:
- Problem clarity (objective, in/out scope, acceptance criteria)
- Context (component/repo mapping, relevant links, business impact)
- Testability (how-to-test definition)
- NFR decision (MVP vs scale-ready)
- Dependencies/risks explicitly called out
If GTD is incomplete, stop early, request clarification, and mark blocked."""

DECISION_GATE_RULES = """Decision Gate:
- Trigger before coding when requirements are ambiguous, conflicting, or architecture/NFR trade-offs are material.
- Present options with trade-offs (MVP vs scale-ready), ask explicit questions, and wait for decision.
- Dev work must not start until Decision Gate is resolved."""

NO_PLACEHOLDERS_RULES = """No placeholders policy:
- No TODO stubs or dummy implementations in production paths.
- If partial work is unavoidable, create a Backlog follow-up issue with exact file paths, impact, and approach.
- Do not auto-promote follow-up issues to executable states."""


def _safe_read(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def build_agent_enforcement_context(*, repo_root: Path) -> str:
    codex_dir = repo_root / ".codex"
    policy_text = _safe_read(codex_dir / "POLICY.md")
    engineering_text = _safe_read(codex_dir / "ENGINEERING_STANDARDS.md")

    policy_pack_payload: dict[str, dict] = {}
    for policy_pack_path in sorted(codex_dir.glob("policy_pack*.json")):
        try:
            policy_pack_payload[policy_pack_path.name] = json.loads(
                policy_pack_path.read_text(encoding="utf-8")
            )
        except json.JSONDecodeError:
            policy_pack_payload[policy_pack_path.name] = {"error": "invalid_json"}

    context_parts = [
        "Run enforcement context (must apply):",
        "",
        GTD_RULES,
        "",
        DECISION_GATE_RULES,
        "",
        NO_PLACEHOLDERS_RULES,
    ]

    if policy_text:
        context_parts.extend(["", "POLICY.md excerpt:", policy_text])
    if engineering_text:
        context_parts.extend(["", "ENGINEERING_STANDARDS.md excerpt:", engineering_text])
    if policy_pack_payload:
        context_parts.extend(
            [
                "",
                "Loaded policy packs:",
                json.dumps(policy_pack_payload, indent=2, sort_keys=True),
            ]
        )

    return "\n".join(context_parts).strip()
