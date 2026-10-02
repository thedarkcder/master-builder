from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

from orchestrator.core.config import Settings
from orchestrator.core.source_layout import require_source_checkout

_SYNC_MANIFEST_NAME = ".repo-sync-manifest.json"
_REPO_CODEX_EXCLUDED_NAMES = {
    ".DS_Store",
    _SYNC_MANIFEST_NAME,
    "auth.json",
    "cache",
    "installation-id",
    "installation-ids.json",
    "installation_id",
    "installation_ids.json",
    "installations",
    "memories",
    "sessions",
    "state",
    "temp",
    "tmp",
}
_RUNTIME_OWNED_CODEX_NAMES = {
    "auth.json",
    "installation-id",
    "installation-ids.json",
    "installation_id",
    "installation_ids.json",
    "installations",
    "state",
}
_RUNTIME_STATEFUL_HOME_PATHS = (
    Path(".cache"),
    Path(".config"),
    Path(".local/share"),
    Path(".local/state"),
)


def _repo_root() -> Path:
    return require_source_checkout()


def _repo_codex_dir() -> Path:
    return _repo_root() / ".codex"


def _legacy_repo_runtime_home(*, runtime_kind: str) -> Path:
    return _repo_root() / ".runtime-home" / runtime_kind


def _running_inside_container() -> bool:
    return Path("/.dockerenv").exists()


def _runtime_scope(*, settings: Settings) -> str:
    raw_value = str(getattr(settings, "agent_id", "") or "").strip()
    if not raw_value:
        return "default"
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", raw_value).strip("-.")
    return normalized or "default"


def resolve_runtime_home(*, settings: Settings) -> Path:
    configured_home = str(getattr(settings, "runtime_home", "") or "").strip()
    if configured_home:
        return Path(configured_home).expanduser()
    normalized_home = str(os.environ.get("HOME") or "").strip()
    if _running_inside_container() and normalized_home:
        return (
            Path(normalized_home)
            / ".codex"
            / "runtime"
            / _runtime_scope(settings=settings)
        )
    base_home = Path(normalized_home).expanduser() if normalized_home else Path.home()
    return base_home / ".master-builder" / "runtime" / _runtime_scope(settings=settings)


def prepare_runtime_home(*, settings: Settings, runtime_kind: str) -> Path:
    if not _repo_codex_dir().is_dir():
        raise RuntimeError(
            "Required .codex runtime assets are missing; restore the full source checkout."
        )
    runtime_home = resolve_runtime_home(settings=settings)
    runtime_home.mkdir(parents=True, exist_ok=True)
    _migrate_legacy_runtime_state(
        runtime_home=runtime_home, settings=settings, runtime_kind=runtime_kind
    )
    _sync_repo_codex_assets(runtime_home=runtime_home)
    return runtime_home


def _migrate_legacy_runtime_state(
    *, runtime_home: Path, settings: Settings, runtime_kind: str
) -> None:
    target_codex_dir = runtime_home / ".codex"
    target_codex_dir.mkdir(parents=True, exist_ok=True)

    home_value = str(os.environ.get("HOME") or "").strip()
    home_path = Path(home_value).expanduser() if home_value else None
    dedicated_runtime_homes: list[Path] = [
        _legacy_repo_runtime_home(runtime_kind=runtime_kind)
    ]
    if home_path is not None:
        dedicated_runtime_homes.append(home_path / runtime_kind)

    for source_home in dedicated_runtime_homes:
        if source_home.resolve() == runtime_home.resolve() or not source_home.exists():
            continue
        _merge_home_state(source_home=source_home, target_home=runtime_home)
        _merge_runtime_owned_codex_entries(
            source_codex_dir=source_home / ".codex", target_codex_dir=target_codex_dir
        )

    if home_path is not None:
        legacy_shared_codex_dir = home_path / ".codex"
        if legacy_shared_codex_dir.exists() and not _is_same_or_nested_path(
            maybe_child=legacy_shared_codex_dir,
            maybe_parent=target_codex_dir,
        ):
            _merge_runtime_owned_codex_entries(
                source_codex_dir=legacy_shared_codex_dir,
                target_codex_dir=target_codex_dir,
            )


def _merge_home_state(*, source_home: Path, target_home: Path) -> None:
    for relative_path in _RUNTIME_STATEFUL_HOME_PATHS:
        source_path = source_home / relative_path
        target_path = target_home / relative_path
        _copy_path_if_missing(source_path=source_path, target_path=target_path)


def _merge_runtime_owned_codex_entries(
    *, source_codex_dir: Path, target_codex_dir: Path
) -> None:
    if not source_codex_dir.is_dir():
        return
    for name in sorted(_RUNTIME_OWNED_CODEX_NAMES):
        _copy_path_if_missing(
            source_path=source_codex_dir / name,
            target_path=target_codex_dir / name,
        )


def _copy_path_if_missing(*, source_path: Path, target_path: Path) -> None:
    if not source_path.exists():
        return
    if source_path.is_file():
        if target_path.exists():
            return
        target_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(source_path, target_path)
        except FileNotFoundError:
            return
        return
    if not source_path.is_dir():
        return
    target_path.mkdir(parents=True, exist_ok=True)
    try:
        children = sorted(source_path.iterdir(), key=lambda item: item.name)
    except FileNotFoundError:
        return
    for child in children:
        _copy_path_if_missing(
            source_path=child,
            target_path=target_path / child.name,
        )


def _sync_repo_codex_assets(*, runtime_home: Path) -> None:
    source_dir = _repo_codex_dir()
    if not source_dir.is_dir():
        raise RuntimeError(
            "Required .codex runtime assets are missing; restore the full source checkout."
        )
    target_dir = runtime_home / ".codex"
    target_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = target_dir / _SYNC_MANIFEST_NAME

    previous_files = _load_sync_manifest(manifest_path=manifest_path)
    synced_files: set[str] = set()
    for source_file in source_dir.rglob("*"):
        if not source_file.is_file():
            continue
        relative_path = source_file.relative_to(source_dir)
        if _should_skip_repo_asset(relative_path):
            continue
        target_file = target_dir / relative_path
        target_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, target_file)
        synced_files.add(relative_path.as_posix())

    for stale_relative in sorted(previous_files - synced_files):
        stale_path = target_dir / Path(stale_relative)
        if stale_path.is_file():
            stale_path.unlink()
        _prune_empty_parent_dirs(stale_path.parent, stop_dir=target_dir)

    manifest_payload = {"repo_managed_files": sorted(synced_files)}
    manifest_path.write_text(
        json.dumps(manifest_payload, indent=2, sort_keys=True), encoding="utf-8"
    )


def _load_sync_manifest(*, manifest_path: Path) -> set[str]:
    if not manifest_path.is_file():
        return set()
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    if not isinstance(payload, dict):
        return set()
    raw_items = payload.get("repo_managed_files")
    if not isinstance(raw_items, list):
        return set()
    normalized: set[str] = set()
    for item in raw_items:
        if isinstance(item, str) and item.strip():
            normalized.add(item.strip())
    return normalized


def _should_skip_repo_asset(relative_path: Path) -> bool:
    parts = {part for part in relative_path.parts if part}
    return bool(parts & _REPO_CODEX_EXCLUDED_NAMES)


def _prune_empty_parent_dirs(start_dir: Path, *, stop_dir: Path) -> None:
    current = start_dir
    while current != stop_dir and current.exists():
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def _is_same_or_nested_path(*, maybe_child: Path, maybe_parent: Path) -> bool:
    try:
        child_resolved = maybe_child.resolve()
        parent_resolved = maybe_parent.resolve()
    except OSError:
        return False
    return (
        child_resolved == parent_resolved or parent_resolved in child_resolved.parents
    )
