#!/usr/bin/env python3
from __future__ import annotations

import ast
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ALLOWLIST_PATH = ROOT / "scripts" / "quality" / "compat_shim_allowlist.txt"
SCAN_ROOT = ROOT / "orchestrator"
EXCLUDED_PATH_PARTS = {"storage/migrations"}
BANNED_IDENTIFIERS = {"InteractiveReplyTransport", "DiscordReplyTransport"}
BANNED_TEXT_PATTERNS = (
    r"\bcompatibility shim\b",
    r"\bbackward[- ]compatibility\b",
)


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


def _is_excluded(path: Path) -> bool:
    relative = path.relative_to(ROOT).as_posix()
    return any(part in relative for part in EXCLUDED_PATH_PARTS)


def _violations_for_path(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    relative = path.relative_to(ROOT).as_posix()
    violations: list[str] = []
    tree = ast.parse(source, filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in BANNED_IDENTIFIERS:
            violations.append(f"{relative}:{node.lineno}:identifier:{node.id}")
        if isinstance(node, ast.Attribute) and node.attr in BANNED_IDENTIFIERS:
            violations.append(f"{relative}:{node.lineno}:attribute:{node.attr}")
    for pattern in BANNED_TEXT_PATTERNS:
        compiled = re.compile(pattern, flags=re.IGNORECASE)
        for match in compiled.finditer(source):
            line = source.count("\n", 0, match.start()) + 1
            violations.append(f"{relative}:{line}:text:{compiled.pattern}")
    return violations


def _is_allowlisted(value: str, patterns: list[re.Pattern[str]]) -> bool:
    return any(pattern.search(value) for pattern in patterns)


def main() -> int:
    allowlist = _allowlist_patterns()
    violations: list[str] = []
    for path in sorted(SCAN_ROOT.rglob("*.py")):
        if _is_excluded(path):
            continue
        for violation in _violations_for_path(path):
            if not _is_allowlisted(violation, allowlist):
                violations.append(violation)
    if violations:
        print("Compatibility shim policy violations detected:")
        for violation in violations:
            print(f"  {violation}")
        print("\nIf intentional, add a precise regex entry to scripts/quality/compat_shim_allowlist.txt.")
        return 1
    print("Compatibility shim policy check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
