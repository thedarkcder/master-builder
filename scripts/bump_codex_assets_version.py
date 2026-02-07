#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

MANIFEST_PATH = Path(".codex/codex_assets_manifest.json")
PYPROJECT_PATH = Path("pyproject.toml")
VERSION_PATTERN = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
PIN_PATTERN = re.compile(r"(master-builder-codex-assets==)([0-9A-Za-z.\-]+)")


def bump_patch(version: str) -> str:
    match = VERSION_PATTERN.fullmatch(version)
    if not match:
        raise ValueError(
            f"Unsupported version '{version}'. Expected stable semantic version format X.Y.Z."
        )
    major, minor, patch = (int(part) for part in match.groups())
    return f"{major}.{minor}.{patch + 1}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bump codex assets base version and sync pyproject dependency pin.",
    )
    parser.add_argument(
        "--set-version",
        dest="set_version",
        default="",
        help="Set explicit stable version (X.Y.Z). If omitted, patch version is bumped.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not MANIFEST_PATH.exists():
        raise FileNotFoundError(f"Missing {MANIFEST_PATH}")
    if not PYPROJECT_PATH.exists():
        raise FileNotFoundError(f"Missing {PYPROJECT_PATH}")

    manifest_payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    current_version = str(manifest_payload.get("assets_version", "")).strip()
    if not current_version:
        raise ValueError("Manifest assets_version is missing or empty")

    target_version = args.set_version.strip() or bump_patch(current_version)
    if not VERSION_PATTERN.fullmatch(target_version):
        raise ValueError(
            f"Invalid target version '{target_version}'. Expected stable semantic version X.Y.Z."
        )

    manifest_payload["assets_version"] = target_version
    MANIFEST_PATH.write_text(
        json.dumps(manifest_payload, indent=2) + "\n",
        encoding="utf-8",
    )

    pyproject_text = PYPROJECT_PATH.read_text(encoding="utf-8")
    updated_pyproject_text, replacements = PIN_PATTERN.subn(
        rf"\g<1>{target_version}",
        pyproject_text,
        count=1,
    )
    if replacements != 1:
        raise ValueError(
            "Could not update master-builder-codex-assets pin in pyproject.toml"
        )
    PYPROJECT_PATH.write_text(updated_pyproject_text, encoding="utf-8")

    print(f"Codex assets version updated: {current_version} -> {target_version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
