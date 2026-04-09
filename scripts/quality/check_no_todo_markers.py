#!/usr/bin/env python3
from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ISSUE_KEY_PATTERN = re.compile(r"\b[A-Z][A-Z0-9_]+-\d+\b")
COMMENT_MARKER_PATTERN = re.compile(r"^\s*(#|//|/\*+|\*+)\s*(TODO|FIXME)\b", re.IGNORECASE)
SCAN_EXTENSIONS = {".py", ".ts", ".tsx", ".js", ".jsx", ".sh"}
SCAN_ROOTS = (ROOT / "orchestrator", ROOT / "admin-ui", ROOT / "deploy")
EXCLUDED_PARTS = {"tests", "__pycache__", "node_modules", ".next", "storage/migrations"}


def _should_skip(path: Path) -> bool:
    relative = path.relative_to(ROOT).as_posix()
    if any(part in relative for part in EXCLUDED_PARTS):
        return True
    return path.suffix.lower() not in SCAN_EXTENSIONS


def main() -> int:
    violations: list[str] = []
    for scan_root in SCAN_ROOTS:
        if not scan_root.exists():
            continue
        for path in sorted(scan_root.rglob("*")):
            if not path.is_file() or _should_skip(path):
                continue
            source = path.read_text(encoding="utf-8")
            for lineno, raw_line in enumerate(source.splitlines(), start=1):
                if not COMMENT_MARKER_PATTERN.search(raw_line):
                    continue
                if ISSUE_KEY_PATTERN.search(raw_line):
                    continue
                relative = path.relative_to(ROOT).as_posix()
                violations.append(f"{relative}:{lineno}:{raw_line.strip()}")
    if violations:
        print("TODO/FIXME markers without issue key detected in production paths:")
        for violation in violations:
            print(f"  {violation}")
        print("\nUse a tracked key in the marker, e.g. `# TODO MAB-123: remove shim`.")
        return 1
    print("No untracked TODO/FIXME markers found in production paths.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
