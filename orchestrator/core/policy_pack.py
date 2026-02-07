from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from orchestrator.tools.github_app import PullRequestFileChange

_LANGUAGE_PRIORITY = (
    "react",
    "python",
    "node",
    "java-spring",
    "android-kotlin",
    "swift",
)

_EXTENSION_LANGUAGE_MAP = {
    ".py": "python",
    ".js": "node",
    ".cjs": "node",
    ".mjs": "node",
    ".ts": "node",
    ".jsx": "react",
    ".tsx": "react",
    ".java": "java-spring",
    ".kt": "android-kotlin",
    ".kts": "android-kotlin",
    ".swift": "swift",
}


@dataclass(frozen=True)
class PolicyPack:
    language_key: str
    name: str
    primary_language: str
    banned_patterns: tuple[str, ...]
    preferred_patterns: tuple[str, ...]
    reviewer_quality_gates: tuple[str, ...]
    source_path: Path


def _policy_pack_path(*, language_key: str, policy_pack_dir: Path) -> Path:
    return policy_pack_dir / f"policy_pack.{language_key}.json"


def load_policy_pack(*, language_key: str, policy_pack_dir: str | Path = ".codex") -> PolicyPack:
    directory = Path(policy_pack_dir)
    source_path = _policy_pack_path(language_key=language_key, policy_pack_dir=directory)
    if not source_path.exists():
        raise FileNotFoundError(f"Policy pack not found: {source_path}")

    parsed = json.loads(source_path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError(f"Policy pack must be a JSON object: {source_path}")

    name = parsed.get("name")
    primary_language = parsed.get("primary_language")
    banned_patterns = parsed.get("banned_patterns")
    preferred_patterns = parsed.get("preferred_patterns")
    reviewer_quality_gates = parsed.get("reviewer_quality_gates")

    if not isinstance(name, str) or not name:
        raise ValueError(f"Policy pack missing 'name': {source_path}")
    if not isinstance(primary_language, str) or not primary_language:
        raise ValueError(f"Policy pack missing 'primary_language': {source_path}")
    if not isinstance(banned_patterns, list):
        raise ValueError(f"Policy pack missing 'banned_patterns' list: {source_path}")
    if not isinstance(preferred_patterns, list):
        preferred_patterns = []
    if not isinstance(reviewer_quality_gates, list):
        reviewer_quality_gates = []

    normalized_banned_patterns = tuple(
        pattern for pattern in banned_patterns if isinstance(pattern, str) and pattern.strip()
    )
    if not normalized_banned_patterns:
        raise ValueError(f"Policy pack has no banned patterns: {source_path}")

    normalized_preferred_patterns = tuple(
        pattern for pattern in preferred_patterns if isinstance(pattern, str) and pattern.strip()
    )
    normalized_quality_gates = tuple(
        gate for gate in reviewer_quality_gates if isinstance(gate, str) and gate.strip()
    )

    return PolicyPack(
        language_key=language_key,
        name=name,
        primary_language=primary_language,
        banned_patterns=normalized_banned_patterns,
        preferred_patterns=normalized_preferred_patterns,
        reviewer_quality_gates=normalized_quality_gates,
        source_path=source_path,
    )


def _detect_language_key_for_filename(filename: str) -> str | None:
    normalized = filename.lower()
    suffix = Path(normalized).suffix
    language_key = _EXTENSION_LANGUAGE_MAP.get(suffix)
    if language_key == "node":
        if "/react/" in normalized or "/frontend/" in normalized or "/ui/" in normalized:
            return "react"
    return language_key


def select_policy_pack_for_files(
    *,
    files: list[PullRequestFileChange],
    policy_pack_dir: str | Path = ".codex",
) -> PolicyPack | None:
    detected_keys: set[str] = set()
    for file_change in files:
        key = _detect_language_key_for_filename(file_change.filename)
        if key:
            detected_keys.add(key)

    if not detected_keys:
        return None

    for key in _LANGUAGE_PRIORITY:
        if key not in detected_keys:
            continue
        source_path = _policy_pack_path(language_key=key, policy_pack_dir=Path(policy_pack_dir))
        if source_path.exists():
            return load_policy_pack(language_key=key, policy_pack_dir=policy_pack_dir)
    return None


def find_banned_pattern_violations(
    *,
    policy_pack: PolicyPack,
    files: list[PullRequestFileChange],
) -> list[str]:
    violations: list[str] = []
    compiled_patterns: list[tuple[str, re.Pattern[str]]] = []
    for pattern in policy_pack.banned_patterns:
        compiled_patterns.append((pattern, re.compile(pattern, flags=re.IGNORECASE | re.MULTILINE)))

    for file_change in files:
        if not file_change.patch:
            continue
        for raw_pattern, compiled in compiled_patterns:
            if compiled.search(file_change.patch):
                violations.append(
                    f"{file_change.filename}: matched banned pattern `{raw_pattern}` "
                    f"from policy pack `{policy_pack.language_key}`"
                )
    return violations
