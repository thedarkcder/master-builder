from __future__ import annotations

import importlib.resources
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
MANIFEST_RELATIVE_PATH = ".codex/codex_assets_manifest.json"

logger = logging.getLogger(__name__)


class EnforcementAssetsError(ValueError):
    """Raised when required enforcement assets are unavailable or invalid."""


def _read_packaged_asset_text(asset_name: str) -> str | None:
    try:
        asset = importlib.resources.files("orchestrator.codex_assets").joinpath(asset_name)
    except (ModuleNotFoundError, FileNotFoundError):
        return None
    if not asset.is_file():
        return None
    content = asset.read_text(encoding="utf-8").strip()
    if not content:
        raise EnforcementAssetsError(f"Packaged enforcement asset is empty: {asset_name}")
    return content


def _load_required_text(*, repo_root: Path, relative_path: str) -> str:
    local_path = repo_root / relative_path
    if local_path.exists():
        content = local_path.read_text(encoding="utf-8").strip()
        if not content:
            raise EnforcementAssetsError(f"Required enforcement file is empty: {relative_path}")
        return content

    asset_name = Path(relative_path).name
    packaged_content = _read_packaged_asset_text(asset_name)
    if packaged_content is not None:
        logger.info(
            "enforcement_context_loaded_packaged_asset relative_path=%s asset=%s",
            relative_path,
            asset_name,
        )
        return packaged_content
    raise EnforcementAssetsError(
        f"Missing required enforcement file: {relative_path} "
        "(not found locally or in packaged codex assets)"
    )


def _load_policy_pack_payload(*, repo_root: Path) -> dict[str, dict]:
    payload: dict[str, dict] = {}
    local_policy_pack_paths = sorted((repo_root / ".codex").glob("policy_pack*.json"))
    if local_policy_pack_paths:
        for path in local_policy_pack_paths:
            content = path.read_text(encoding="utf-8").strip()
            if not content:
                raise EnforcementAssetsError(f"Policy pack is empty: {path.name}")
            try:
                payload[path.name] = json.loads(content)
            except json.JSONDecodeError as exc:
                raise EnforcementAssetsError(f"Policy pack is invalid JSON: {path.name}") from exc
        return payload

    try:
        packaged_dir = importlib.resources.files("orchestrator.codex_assets")
        packaged_policy_pack_assets = sorted(
            (
                resource
                for resource in packaged_dir.iterdir()
                if resource.name.startswith("policy_pack") and resource.name.endswith(".json")
            ),
            key=lambda resource: resource.name,
        )
    except (ModuleNotFoundError, FileNotFoundError):
        packaged_policy_pack_assets = []

    for resource in packaged_policy_pack_assets:
        content = resource.read_text(encoding="utf-8").strip()
        if not content:
            raise EnforcementAssetsError(f"Packaged policy pack is empty: {resource.name}")
        try:
            payload[resource.name] = json.loads(content)
        except json.JSONDecodeError as exc:
            raise EnforcementAssetsError(f"Packaged policy pack is invalid JSON: {resource.name}") from exc

    if not payload:
        raise EnforcementAssetsError(
            "Missing policy packs: expected .codex/policy_pack*.json locally or packaged codex assets"
        )
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
    assets_version = validate_enforcement_assets(
        repo_root=repo_root,
        required_assets_version=required_assets_version,
    )

    context_parts = ["Run enforcement context (must apply):", f"Codex assets version: {assets_version}"]
    for relative_path, content in guidance_text.items():
        context_parts.extend(["", f"{relative_path} excerpt:", content])
    context_parts.extend(["", "Loaded policy packs:", json.dumps(policy_pack_payload, indent=2, sort_keys=True)])
    return "\n".join(context_parts).strip()
