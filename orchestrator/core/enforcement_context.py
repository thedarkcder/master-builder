from __future__ import annotations

import json
from pathlib import Path

REQUIRED_GUIDANCE_FILES = (
    ".codex/POLICY.md",
    ".codex/ENGINEERING_STANDARDS.md",
    ".codex/DECISION_GATE_TEMPLATE.md",
)


def _read_required(repo_root: Path, relative_path: str) -> str:
    path = repo_root / relative_path
    if not path.exists():
        raise FileNotFoundError(f"Missing required enforcement file: {relative_path}")
    content = path.read_text(encoding="utf-8").strip()
    if not content:
        raise ValueError(f"Required enforcement file is empty: {relative_path}")
    return content


def build_agent_enforcement_context(*, repo_root: Path) -> str:
    codex_dir = repo_root / ".codex"
    guidance_text = {
        relative_path: _read_required(repo_root, relative_path)
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
