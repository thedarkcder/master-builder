from __future__ import annotations

import re
from typing import Iterable, Sequence

_RELEASE_TAG_PATTERN = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


def normalize_release_version(version: str) -> str:
    value = version.strip()
    if not value:
        raise ValueError("Release version cannot be empty")
    if not value.startswith("v"):
        value = f"v{value}"
    match = _RELEASE_TAG_PATTERN.fullmatch(value)
    if not match:
        raise ValueError(f"Invalid release version '{version}'. Expected vX.Y.Z")
    major, minor, patch = (int(part) for part in match.groups())
    return f"v{major}.{minor}.{patch}"


def next_release_version_from_tags(tags: Sequence[str]) -> str:
    parsed: list[tuple[int, int, int]] = []
    for tag in tags:
        candidate = tag.strip()
        match = _RELEASE_TAG_PATTERN.fullmatch(candidate)
        if not match:
            continue
        parsed.append(tuple(int(part) for part in match.groups()))

    if not parsed:
        return "v0.1.0"

    major, minor, patch = max(parsed)
    return f"v{major}.{minor}.{patch + 1}"


def release_label_for_version(version: str) -> str:
    return f"release:{normalize_release_version(version)}"


def apply_release_label(labels: Iterable[str], *, target_label: str) -> list[str]:
    cleaned = [label for label in labels if not label.startswith("release:")]
    if target_label not in cleaned:
        cleaned.append(target_label)
    return cleaned
