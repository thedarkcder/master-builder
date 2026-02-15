from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

REQUIRED_GUIDANCE_FILES = (
    ".codex/POLICY.md",
    ".codex/ENGINEERING_STANDARDS.md",
    ".codex/OPERATING.md",
    ".codex/DECISION_GATE_TEMPLATE.md",
    ".codex/PR_READY_TEMPLATES.md",
)
MANIFEST_RELATIVE_PATH = ".codex/codex_assets_manifest.json"
OPTIONAL_GUIDANCE_FILES = ("AGENTS.md",)
SKILL_RELATIVE_GLOB = ".codex/skills/**/SKILL.md"
USER_SKILL_RELATIVE_GLOB = ".codex/skills/**/SKILL.md"

class EnforcementAssetsError(ValueError):
    """Raised when required enforcement assets are unavailable or invalid."""


def _load_required_text(*, repo_root: Path, relative_path: str) -> str:
    local_path = repo_root / relative_path
    if local_path.exists():
        content = local_path.read_text(encoding="utf-8").strip()
        if not content:
            raise EnforcementAssetsError(f"Required enforcement file is empty: {relative_path}")
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


def _iter_skill_paths(*, repo_root: Path) -> Iterable[Path]:
    repo_skill_paths = list((repo_root / ".codex" / "skills").glob("**/SKILL.md"))
    user_skill_root = Path.home() / ".codex" / "skills"
    user_skill_paths = list(user_skill_root.glob("**/SKILL.md")) if user_skill_root.exists() else []
    seen: set[str] = set()
    for skill_path in sorted([*repo_skill_paths, *user_skill_paths], key=lambda p: str(p)):
        key = str(skill_path.resolve()) if skill_path.exists() else str(skill_path)
        if key in seen:
            continue
        seen.add(key)
        yield skill_path


def _load_skill_texts(*, repo_root: Path) -> list[tuple[str, str]]:
    payload: list[tuple[str, str]] = []
    for skill_path in _iter_skill_paths(repo_root=repo_root):
        if not skill_path.exists():
            continue
        content = skill_path.read_text(encoding="utf-8").strip()
        if not content:
            continue
        try:
            relative_to_repo = skill_path.relative_to(repo_root)
            display_path = str(relative_to_repo)
        except ValueError:
            display_path = str(skill_path)
        payload.append((display_path, content))
    return payload


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
            raise EnforcementAssetsError(f"Policy pack is invalid JSON: {path.name}") from exc

    if not payload:
        raise EnforcementAssetsError("Missing policy packs: expected .codex/policy_pack*.json in repository")
    return payload


def _load_assets_manifest(*, repo_root: Path) -> dict[str, object]:
    manifest_content = _load_required_text(repo_root=repo_root, relative_path=MANIFEST_RELATIVE_PATH)
    try:
        manifest_payload = json.loads(manifest_content)
    except json.JSONDecodeError as exc:
        raise EnforcementAssetsError(
            f"Invalid codex assets manifest JSON: {MANIFEST_RELATIVE_PATH}"
        ) from exc
    if not isinstance(manifest_payload, dict):
        raise EnforcementAssetsError("Invalid codex assets manifest payload: expected JSON object")
    return manifest_payload


def codex_assets_version(*, repo_root: Path) -> str:
    manifest_payload = _load_assets_manifest(repo_root=repo_root)
    raw_version = manifest_payload.get("assets_version")
    if not isinstance(raw_version, str) or not raw_version.strip():
        raise EnforcementAssetsError("Codex assets manifest missing non-empty 'assets_version'")
    return raw_version.strip()


def validate_enforcement_assets(
    *,
    repo_root: Path,
    required_assets_version: str | None = None,
) -> str:
    for relative_path in REQUIRED_GUIDANCE_FILES:
        _load_required_text(repo_root=repo_root, relative_path=relative_path)
    _load_policy_pack_payload(repo_root=repo_root)
    detected_version = codex_assets_version(repo_root=repo_root)

    expected_version = (required_assets_version or "").strip()
    if expected_version and detected_version != expected_version:
        raise EnforcementAssetsError(
            f"Codex assets version mismatch: required={expected_version}, found={detected_version}"
        )
    return detected_version


def build_agent_enforcement_context(
    *,
    repo_root: Path,
    required_assets_version: str | None = None,
) -> str:
    guidance_text = {
        relative_path: _load_required_text(repo_root=repo_root, relative_path=relative_path)
        for relative_path in REQUIRED_GUIDANCE_FILES
    }
    policy_pack_payload = _load_policy_pack_payload(repo_root=repo_root)
    assets_version = codex_assets_version(repo_root=repo_root)
    expected_version = (required_assets_version or "").strip()
    if expected_version and assets_version != expected_version:
        raise EnforcementAssetsError(
            f"Codex assets version mismatch: required={expected_version}, found={assets_version}"
        )

    context_parts = ["Run enforcement context (must apply):", f"Codex assets version: {assets_version}"]
    for relative_path, content in guidance_text.items():
        context_parts.extend(["", f"{relative_path} excerpt:", content])
    for relative_path in OPTIONAL_GUIDANCE_FILES:
        optional_content = _load_optional_text(repo_root=repo_root, relative_path=relative_path)
        if optional_content:
            context_parts.extend(["", f"{relative_path} excerpt:", optional_content])

    skill_texts = _load_skill_texts(repo_root=repo_root)
    if skill_texts:
        context_parts.extend(["", "Loaded skills:"])
        for skill_path, skill_content in skill_texts:
            context_parts.extend(["", f"{skill_path} excerpt:", skill_content])
    context_parts.extend(["", "Loaded policy packs:", json.dumps(policy_pack_payload, indent=2, sort_keys=True)])
    return "\n".join(context_parts).strip()
