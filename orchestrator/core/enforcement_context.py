from __future__ import annotations

import json
import logging
from pathlib import Path

REQUIRED_GUIDANCE_FILES = (
    ".codex/POLICY.md",
    ".codex/ENGINEERING_STANDARDS.md",
    ".codex/OPERATING.md",
    ".codex/DECISION_GATE_TEMPLATE.md",
    ".codex/PR_READY_TEMPLATES.md",
)

logger = logging.getLogger(__name__)

FALLBACK_GUIDANCE: dict[str, str] = {
    ".codex/POLICY.md": (
        "Fallback POLICY guidance (packaged mode):\n"
        "- Apply Good To Do checks before execution.\n"
        "- Do not ship placeholder/TODO implementations in production paths.\n"
        "- Never auto-merge pull requests."
    ),
    ".codex/ENGINEERING_STANDARDS.md": (
        "Fallback ENGINEERING_STANDARDS guidance (packaged mode):\n"
        "- Avoid time-based synchronization (sleep/setTimeout) for coordination.\n"
        "- Prefer deterministic tests and explicit state transitions."
    ),
    ".codex/OPERATING.md": (
        "Fallback OPERATING guidance (packaged mode):\n"
        "- Execute only Jira items that are explicitly ready for execution.\n"
        "- If blocked, move work to Blocked with a clear unblock dependency."
    ),
    ".codex/DECISION_GATE_TEMPLATE.md": (
        "Fallback Decision Gate template (packaged mode):\n"
        "Decision Gate\n"
        "- Why a decision is required\n"
        "- Options (MVP vs scale-ready)\n"
        "- Questions (max 5)\n"
        "- Recommendation"
    ),
    ".codex/PR_READY_TEMPLATES.md": (
        "Fallback PR Ready template (packaged mode):\n"
        "- Include summary, risks, and how to test.\n"
        "- Do not mark PR Ready until CI and Security checks are green."
    ),
}


def _read_guidance(repo_root: Path, relative_path: str) -> str:
    path = repo_root / relative_path
    if path.exists():
        content = path.read_text(encoding="utf-8").strip()
        if content:
            return content
        logger.warning("enforcement_context_empty_guidance_file path=%s", relative_path)
    else:
        logger.warning("enforcement_context_missing_guidance_file path=%s", relative_path)

    fallback = FALLBACK_GUIDANCE.get(relative_path)
    if fallback:
        return fallback
    return (
        "Fallback guidance unavailable. Continue with packaged defaults and policy-pack checks."
    )


def build_agent_enforcement_context(*, repo_root: Path) -> str:
    codex_dir = repo_root / ".codex"
    guidance_text = {
        relative_path: _read_guidance(repo_root, relative_path)
        for relative_path in REQUIRED_GUIDANCE_FILES
    }

    policy_pack_payload: dict[str, dict] = {}
    for policy_pack_path in sorted(codex_dir.glob("policy_pack*.json")):
        try:
            policy_pack_payload[policy_pack_path.name] = json.loads(
                policy_pack_path.read_text(encoding="utf-8")
            )
        except json.JSONDecodeError:
            policy_pack_payload[policy_pack_path.name] = {"error": "invalid_json"}

    context_parts = ["Run enforcement context (must apply):"]
    for relative_path, content in guidance_text.items():
        context_parts.extend(["", f"{relative_path} excerpt:", content])
    if policy_pack_payload:
        context_parts.extend(
            [
                "",
                "Loaded policy packs:",
                json.dumps(policy_pack_payload, indent=2, sort_keys=True),
            ]
        )

    return "\n".join(context_parts).strip()
