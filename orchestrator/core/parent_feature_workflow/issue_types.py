from __future__ import annotations

SELF_EXECUTABLE_PARENT_ISSUE_TYPES = frozenset({"task", "bug"})


def normalized_issue_type(value: object) -> str:
    return str(value or "").strip().casefold()


def is_self_executable_parent_issue_type(value: object) -> bool:
    return normalized_issue_type(value) in SELF_EXECUTABLE_PARENT_ISSUE_TYPES
