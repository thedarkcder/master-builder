#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUFF_RULES = "B,UP,SIM,C4,ARG,PL,RUF"


def _base_ref() -> str | None:
    base_ref = str(os.environ.get("GITHUB_BASE_REF") or "").strip()
    if not base_ref:
        return None
    return base_ref


def _fetch_base(base_ref: str) -> None:
    subprocess.run(
        ["git", "fetch", "--no-tags", "--depth=1", "origin", base_ref],
        cwd=ROOT,
        check=True,
    )


def _changed_python_files(base_ref: str) -> list[str]:
    proc = subprocess.run(
        [
            "git",
            "diff",
            "--name-only",
            "--diff-filter=AM",
            f"origin/{base_ref}...HEAD",
            "--",
            "*.py",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _changed_line_map(base_ref: str, files: list[str]) -> dict[str, set[int]]:
    if not files:
        return {}
    proc = subprocess.run(
        ["git", "diff", "-U0", f"origin/{base_ref}...HEAD", "--", *files],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    changed: dict[str, set[int]] = defaultdict(set)
    current_file: str | None = None
    hunk_pattern = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
    for line in proc.stdout.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:].strip()
            continue
        match = hunk_pattern.match(line)
        if not match or current_file is None:
            continue
        start = int(match.group(1))
        count = int(match.group(2) or "1")
        for row in range(start, start + max(0, count)):
            changed[current_file].add(row)
    return changed


def _ruff_violations(files: list[str]) -> list[dict]:
    if not files:
        return []
    proc = subprocess.run(
        [
            "ruff",
            "check",
            "--select",
            RUFF_RULES,
            "--ignore",
            "E501",
            "--output-format",
            "json",
            *files,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if not proc.stdout.strip():
        return []
    return json.loads(proc.stdout)


def main() -> int:
    base_ref = _base_ref()
    if base_ref is None:
        print("Strict Ruff changed-lines check skipped (GITHUB_BASE_REF is not set).")
        return 0

    _fetch_base(base_ref)
    files = _changed_python_files(base_ref)
    if not files:
        print("Strict Ruff changed-lines check skipped (no added/modified Python files).")
        return 0

    changed_rows = _changed_line_map(base_ref, files)
    violations = _ruff_violations(files)
    blocking: list[dict] = []
    for violation in violations:
        filename = str(violation.get("filename") or "")
        row = int((violation.get("location") or {}).get("row") or 0)
        if row in changed_rows.get(filename, set()):
            blocking.append(violation)

    if not blocking:
        print("Strict Ruff changed-lines check passed.")
        return 0

    print("Strict Ruff violations on changed lines:")
    for violation in blocking:
        filename = violation.get("filename")
        row = (violation.get("location") or {}).get("row")
        col = (violation.get("location") or {}).get("column")
        code = violation.get("code")
        message = violation.get("message")
        print(f"  {filename}:{row}:{col} {code} {message}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
