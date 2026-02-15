from __future__ import annotations

from typing import Any


def normalize_seed_issue_labels(raw_labels: Any) -> list[str]:
    if not isinstance(raw_labels, list):
        return ["discord-seeded"]
    normalized: list[str] = ["discord-seeded"]
    for label in raw_labels:
        text = str(label).strip().lower()
        if text and text not in normalized:
            normalized.append(text)
    return normalized


def normalize_seed_issue_tags(raw_tags: Any) -> list[str]:
    if not isinstance(raw_tags, list):
        return []
    normalized: list[str] = []
    for tag in raw_tags:
        text = str(tag).strip().lower()
        if text and text not in normalized:
            normalized.append(text)
    return normalized


def parse_seed_issue_type(raw_issue_type: Any) -> str:
    normalized = str(raw_issue_type or "").strip().lower()
    if normalized == "bug":
        return "Bug"
    if normalized == "story":
        return "Story"
    return "Task"


def normalize_seed_issue_scope(raw_scope: Any) -> list[str]:
    if not isinstance(raw_scope, list):
        return []
    return [str(item).strip() for item in raw_scope if str(item).strip()]


def normalize_seed_issue_key(raw_issue_key: Any, *, issue_key_pattern) -> str | None:  # noqa: ANN001
    normalized = str(raw_issue_key or "").strip().upper()
    if issue_key_pattern.match(normalized):
        return normalized
    return None


def seed_text_is_missing(value: str) -> bool:
    normalized = value.strip().lower()
    if not normalized:
        return True
    if normalized in {"tbd", "unknown", "n/a", "na", "none", "todo", "decide later"}:
        return True
    if "to be determined" in normalized:
        return True
    if "???" in normalized:
        return True
    return False


def collect_seed_issue_questions(
    *,
    issue_summary: str,
    objective: str,
    scope_in: list[str],
    scope_out: list[str],
    acceptance: list[str],
    how_to_test: list[str],
    nfr_intent: str,
) -> list[str]:
    questions: list[str] = []
    if seed_text_is_missing(objective):
        questions.append("What is the objective for this issue in one sentence?")
    if not scope_in:
        questions.append("What is explicitly in scope for this issue?")
    if not scope_out:
        questions.append("What is explicitly out of scope for this issue?")
    if not acceptance:
        questions.append("What acceptance criteria must be satisfied for this issue?")
    if not how_to_test:
        questions.append("What exact validation steps/commands should be used to test completion?")
    if seed_text_is_missing(nfr_intent):
        questions.append("Should this be MVP quick delivery or scale-ready design?")
    return questions
