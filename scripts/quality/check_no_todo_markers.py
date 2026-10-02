#!/usr/bin/env python3
from __future__ import annotations

import re
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ISSUE_KEY_PATTERN = re.compile(r"\b[A-Z][A-Z0-9_]+-\d+\b")
COMMENT_MARKER_PATTERN = re.compile(r"(#|//|/\*+|\*+)\s*(TODO|FIXME)\b", re.IGNORECASE)
SCAN_EXTENSIONS = {".py", ".ts", ".tsx", ".js", ".jsx", ".sh"}
SCAN_ROOTS = (ROOT / "orchestrator", ROOT / "admin-ui", ROOT / "deploy")
EXCLUDED_PARTS = {
    "tests",
    "__pycache__",
    "node_modules",
    ".next",
    ".next-playwright",
    "test-results",
    "playwright-report",
    "storage/migrations",
}


def _raise_scan_error(error: OSError) -> None:
    raise error


def _production_files(scan_root: Path):
    # Prune generated output before walking it: browser runs replace these
    # directories concurrently and their contents are not production source.
    for current, directories, filenames in os.walk(
        scan_root, onerror=_raise_scan_error
    ):
        current_path = Path(current)
        directories[:] = [
            name
            for name in sorted(directories)
            if name not in EXCLUDED_PARTS
            and (current_path / name).relative_to(ROOT).as_posix()
            != "orchestrator/storage/migrations"
        ]
        for filename in sorted(filenames):
            path = current_path / filename
            if not _should_skip(path):
                yield path


def _should_skip(path: Path) -> bool:
    relative = path.relative_to(ROOT).as_posix()
    if any(part in relative for part in EXCLUDED_PARTS):
        return True
    return path.suffix.lower() not in SCAN_EXTENSIONS


def _has_untracked_todo_marker(line: str) -> bool:
    marker = COMMENT_MARKER_PATTERN.search(line)
    if marker is None:
        return False
    if ISSUE_KEY_PATTERN.search(line):
        return False
    return True


def main() -> int:
    violations: list[str] = []
    for scan_root in SCAN_ROOTS:
        if not scan_root.exists():
            continue
        for path in _production_files(scan_root):
            source = path.read_text(encoding="utf-8")
            for lineno, raw_line in enumerate(source.splitlines(), start=1):
                if not _has_untracked_todo_marker(raw_line):
                    continue
                relative = path.relative_to(ROOT).as_posix()
                violations.append(f"{relative}:{lineno}:{raw_line.strip()}")
    if violations:
        print("TODO/FIXME markers without issue key detected in production paths:")
        for violation in violations:
            print(f"  {violation}")
        print(
            "\nUse a tracked key in the marker, e.g. `# TODO EXAMPLE-123: remove shim`."
        )
        return 1
    print("No untracked TODO/FIXME markers found in production paths.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
