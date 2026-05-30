from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

WorkItemKind = Literal["parent", "child"]

_PARENT_PREFIX = "parent:"
_CHILD_PREFIX = "child:"


@dataclass(frozen=True)
class ExecutableWorkItemRef:
    kind: WorkItemKind
    execution_id: str
    issue_key: str | None = None


def _required_text(value: str, *, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    if ":" in normalized and field_name == "issue_key":
        raise ValueError("issue_key must not contain ':'")
    return normalized


def parent_work_item_id(*, execution_id: str) -> str:
    return f"{_PARENT_PREFIX}{_required_text(execution_id, field_name='execution_id')}"


def child_work_item_id(*, execution_id: str, issue_key: str) -> str:
    normalized_execution_id = _required_text(execution_id, field_name="execution_id")
    normalized_issue_key = _required_text(issue_key, field_name="issue_key").upper()
    return f"{_CHILD_PREFIX}{normalized_execution_id}:{normalized_issue_key}"


def parse_work_item_id(work_item_id: str) -> ExecutableWorkItemRef:
    normalized = str(work_item_id or "").strip()
    if normalized.startswith(_PARENT_PREFIX):
        execution_id = _required_text(normalized[len(_PARENT_PREFIX) :], field_name="execution_id")
        return ExecutableWorkItemRef(kind="parent", execution_id=execution_id)
    if normalized.startswith(_CHILD_PREFIX):
        remainder = normalized[len(_CHILD_PREFIX) :]
        execution_id, separator, issue_key = remainder.rpartition(":")
        if not separator:
            raise ValueError("Child work item id must include execution_id and issue_key")
        return ExecutableWorkItemRef(
            kind="child",
            execution_id=_required_text(execution_id, field_name="execution_id"),
            issue_key=_required_text(issue_key, field_name="issue_key").upper(),
        )
    raise ValueError("Unknown work item id")
