from __future__ import annotations

import json
from pathlib import Path

REQUIRED_GUIDANCE_FILES = (
    ".codex/POLICY.md",
    ".codex/ENGINEERING_STANDARDS.md",
    ".codex/OPERATING.md",
    ".codex/DECISION_GATE_TEMPLATE.md",
    ".codex/PR_READY_TEMPLATES.md",
)
OPTIONAL_GUIDANCE_FILES = ("AGENTS.md",)


class EnforcementAssetsError(ValueError):
    """Raised when required enforcement assets are unavailable or invalid."""


def _load_required_text(*, repo_root: Path, relative_path: str) -> str:
    local_path = repo_root / relative_path
    if local_path.exists():
        content = local_path.read_text(encoding="utf-8").strip()
        if not content:
            raise EnforcementAssetsError(
                f"Required enforcement file is empty: {relative_path}"
            )
        return content
    raise EnforcementAssetsError(
        f"Missing required enforcement file: {relative_path} (not found in repository)"
    )


def _load_optional_text(*, repo_root: Path, relative_path: str) -> str | None:
    local_path = repo_root / relative_path
    if local_path.exists():
        content = local_path.read_text(encoding="utf-8").strip()
        return content or None
    return None


def _load_policy_pack_payload(*, repo_root: Path) -> dict[str, dict]:
    payload: dict[str, dict] = {}
    local_policy_pack_paths = sorted((repo_root / ".codex").glob("policy_pack*.json"))
    for path in local_policy_pack_paths:
        content = path.read_text(encoding="utf-8").strip()
        if not content:
            raise EnforcementAssetsError(f"Policy pack is empty: {path.name}")
        try:
            payload[path.name] = json.loads(content)
        except json.JSONDecodeError as exc:
            raise EnforcementAssetsError(
                f"Policy pack is invalid JSON: {path.name}"
            ) from exc

    if not payload:
        raise EnforcementAssetsError(
            "Missing policy packs: expected .codex/policy_pack*.json in repository"
        )
    return payload


def validate_enforcement_assets(
    *,
    repo_root: Path,
) -> None:
    for relative_path in REQUIRED_GUIDANCE_FILES:
        _load_required_text(repo_root=repo_root, relative_path=relative_path)
    _load_policy_pack_payload(repo_root=repo_root)


def build_agent_enforcement_context(
    *,
    repo_root: Path,
) -> str:
    guidance_text = {
        relative_path: _load_required_text(
            repo_root=repo_root, relative_path=relative_path
        )
        for relative_path in REQUIRED_GUIDANCE_FILES
    }
    policy_pack_payload = _load_policy_pack_payload(repo_root=repo_root)

    context_parts = ["Run enforcement context (must apply):"]
    for relative_path, content in guidance_text.items():
        context_parts.extend(["", f"{relative_path} excerpt:", content])
    for relative_path in OPTIONAL_GUIDANCE_FILES:
        optional_content = _load_optional_text(
            repo_root=repo_root, relative_path=relative_path
        )
        if optional_content:
            context_parts.extend(["", f"{relative_path} excerpt:", optional_content])
    context_parts.extend(
        [
            "",
            "Loaded policy packs:",
            json.dumps(policy_pack_payload, indent=2, sort_keys=True),
        ]
    )
    return "\n".join(context_parts).strip()
