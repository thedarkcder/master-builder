#!/usr/bin/env python3
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ALLOWLIST_PATH = ROOT / "scripts" / "quality" / "vulture_allowlist.txt"
TARGETS = ("orchestrator",)


def _allowlist_patterns() -> list[re.Pattern[str]]:
    if not ALLOWLIST_PATH.exists():
        return []
    patterns: list[re.Pattern[str]] = []
    for raw in ALLOWLIST_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append(re.compile(line))
    return patterns


def _is_allowlisted(line: str, patterns: list[re.Pattern[str]]) -> bool:
    return any(pattern.search(line) for pattern in patterns)


def main() -> int:
    cmd = [
        sys.executable,
        "-m",
        "vulture",
        *TARGETS,
        "--min-confidence",
        "95",
        "--sort-by-size",
    ]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=False)
    output_lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    patterns = _allowlist_patterns()
    violations = [line for line in output_lines if not _is_allowlisted(line, patterns)]
    if violations:
        print("Dead code candidates detected (not allowlisted):")
        for line in violations:
            print(f"  {line}")
        print(
            "\nIf intentional, add a precise regex entry to scripts/quality/vulture_allowlist.txt."
        )
        return 1
    if proc.returncode not in (0, 1):
        sys.stderr.write(proc.stderr)
        return proc.returncode
    print("Dead code check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
