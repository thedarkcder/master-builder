from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from orchestrator.core.issue_fanout.description import build_engineering_child_description
from orchestrator.core.issue_fanout.draft_assembly import normalize_planning_package

_PM_PARENT_LABEL = "pm-parent"
_ENGINEERING_CHILD_LABEL = "engineering-child"
_SYNC_LABELS = {"sync-current", "sync-stale", "sync-blocked"}


@dataclass(frozen=True)
class SelfExecutableIssueContract:
    issue_key: str
    summary: str
    description: str
    labels: list[str]


def _normalized_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split()).strip()


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    normalized: list[str] = []
    for item in value:
        text = _normalized_text(item)
        if text:
            normalized.append(text)
    return normalized


def _first_non_empty(*values: object) -> str:
    for value in values:
        text = _normalized_text(value)
        if text:
            return text
    return ""


def _require_text(*, value: object, field_name: str, issue_key: str) -> str:
    text = _normalized_text(value)
    if not text:
        raise RuntimeError(f"Self-executable issue contract for {issue_key} requires {field_name}")
    return text


def _require_string_list(*, value: object, field_name: str, issue_key: str) -> list[str]:
    values = _string_list(value)
    if not values:
        raise RuntimeError(f"Self-executable issue contract for {issue_key} requires {field_name}")
    return values


def _adf_plain_text(node: object) -> str:
    if isinstance(node, str):
        return node.strip()
    if isinstance(node, list):
        return "\n".join(part for part in (_adf_plain_text(item) for item in node) if part).strip()
    if not isinstance(node, dict):
        return ""
    text = str(node.get("text") or "").strip()
    if text:
        return text
    content = node.get("content")
    if isinstance(content, list):
        return "\n".join(part for part in (_adf_plain_text(item) for item in content) if part).strip()
    return ""


def _testing_lines(planning_package: dict[str, Any]) -> list[str]:
    specialist_outputs = planning_package.get("specialist_outputs")
    if not isinstance(specialist_outputs, dict):
        return []
    testing_output = specialist_outputs.get("testing")
    if not isinstance(testing_output, dict):
        return []
    lines: list[str] = []
    for field_name in ("recommendations", "required_tasks", "acceptance_impacts"):
        lines.extend(_string_list(testing_output.get(field_name)))
    return lines


def _dependencies_and_risks(product_brief: dict[str, Any]) -> list[str]:
    combined = [
        *_string_list(product_brief.get("dependencies_and_risks")),
        *_string_list(product_brief.get("dependencies")),
        *_string_list(product_brief.get("risks")),
        *_string_list(product_brief.get("constraints")),
    ]
    deduped: list[str] = []
    seen: set[str] = set()
    for item in combined:
        key = item.casefold()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _self_executable_labels(existing_labels: list[str] | tuple[str, ...] | None) -> list[str]:
    _ = existing_labels
    return [_ENGINEERING_CHILD_LABEL, "sync-current"]


def build_self_executable_issue_contract(
    *,
    parent_detail,
    product_brief: dict[str, Any],
    planning_package: dict[str, Any],
    parent_revision: str,
    planning_state: str | None = None,
) -> SelfExecutableIssueContract:
    issue_key = _normalized_text(getattr(parent_detail, "key", None))
    summary = _normalized_text(getattr(parent_detail, "summary", None))
    if not issue_key:
        raise RuntimeError("Self-executable issue contract requires a Jira issue key")
    if not summary:
        raise RuntimeError(f"Self-executable issue contract requires a Jira summary for {issue_key}")
    if not isinstance(product_brief, dict):
        raise RuntimeError(f"Self-executable issue contract requires a normalized product brief for {issue_key}")
    if not isinstance(planning_package, dict):
        raise RuntimeError(f"Self-executable issue contract requires a planning package for {issue_key}")

    normalized_parent_revision = _require_text(
        value=parent_revision,
        field_name="parent_revision",
        issue_key=issue_key,
    )
    planning_draft = normalize_planning_package(planning_package)
    capability = _require_text(value=product_brief.get("objective"), field_name="objective", issue_key=issue_key)
    delivery = _require_text(value=product_brief.get("recommendation"), field_name="recommendation", issue_key=issue_key)
    expected_outcome = _require_text(value=product_brief.get("user_value"), field_name="user_value", issue_key=issue_key)
    acceptance_criteria = _require_string_list(
        value=product_brief.get("acceptance_criteria"),
        field_name="acceptance_criteria",
        issue_key=issue_key,
    )
    success_outcomes = _require_string_list(
        value=product_brief.get("success_outcomes"),
        field_name="success_outcomes",
        issue_key=issue_key,
    )
    description_adf = build_engineering_child_description(
        parent_issue_key=issue_key,
        parent_summary=summary,
        parent_revision=normalized_parent_revision,
        capability=capability,
        delivery=delivery,
        expected_outcome=expected_outcome,
        acceptance_criteria=acceptance_criteria,
        how_to_test=[*_string_list(product_brief.get("how_to_test")), *_testing_lines(planning_package)],
        done_means=[
            *success_outcomes,
            *acceptance_criteria,
        ],
        dependencies_and_risks=_dependencies_and_risks(product_brief),
        specialist_summary=list(planning_draft.specialist_summary),
        technical_decisions=list(planning_draft.technical_decisions),
        planning_state=_first_non_empty(planning_state, planning_draft.planning_state_for_description)
        or None,
    )
    description = _adf_plain_text(description_adf)
    if not description:
        raise RuntimeError(f"Self-executable issue contract for {issue_key} produced an empty description")
    return SelfExecutableIssueContract(
        issue_key=issue_key,
        summary=summary,
        description=description,
        labels=_self_executable_labels(getattr(parent_detail, "labels", None)),
    )
