from __future__ import annotations

from dataclasses import dataclass


PRECHECK_QUESTIONS_BLOCK_START = "<!-- precheck-questions:start -->"
PRECHECK_QUESTIONS_BLOCK_END = "<!-- precheck-questions:end -->"
_DECISION_GATE_TAG = "[decision_gate]"
_GTD_TAG = "[gtd]"


@dataclass(frozen=True)
class LockedPrecheckQuestions:
    decision_gate_reason: str | None
    decision_gate_questions: tuple[str, ...]
    gtd_questions: tuple[str, ...]


def _normalize_questions(questions: list[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for item in questions:
        question = str(item or "").strip()
        if not question:
            continue
        folded = question.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        normalized.append(question)
    return tuple(normalized)


def build_precheck_questions_block(
    *,
    decision_gate_reason: str | None,
    decision_gate_questions: list[str],
    gtd_questions: list[str],
) -> str:
    normalized_reason = str(decision_gate_reason or "").strip() or None
    normalized_decision_gate = _normalize_questions(decision_gate_questions)
    normalized_gtd = _normalize_questions(gtd_questions)
    if not normalized_reason and not normalized_decision_gate and not normalized_gtd:
        return ""
    lines = [
        PRECHECK_QUESTIONS_BLOCK_START,
        "## Precheck Clarification Questions",
    ]
    if normalized_reason:
        lines.append(f"Decision Gate reason: {normalized_reason}")
    for question in normalized_decision_gate:
        lines.append(f"- {_DECISION_GATE_TAG} {question}")
    for question in normalized_gtd:
        lines.append(f"- {_GTD_TAG} {question}")
    lines.append(PRECHECK_QUESTIONS_BLOCK_END)
    return "\n".join(lines)


def _extract_block(description: str) -> str | None:
    start_idx = description.find(PRECHECK_QUESTIONS_BLOCK_START)
    end_idx = description.find(PRECHECK_QUESTIONS_BLOCK_END, start_idx + len(PRECHECK_QUESTIONS_BLOCK_START))
    if start_idx < 0 or end_idx <= start_idx:
        return None
    return description[start_idx : end_idx + len(PRECHECK_QUESTIONS_BLOCK_END)]


def parse_locked_precheck_questions(*, issue_description: str | None) -> LockedPrecheckQuestions:
    description = str(issue_description or "")
    block = _extract_block(description)
    if not block:
        return LockedPrecheckQuestions(
            decision_gate_reason=None,
            decision_gate_questions=(),
            gtd_questions=(),
        )

    decision_gate_reason: str | None = None
    decision_gate_questions: list[str] = []
    gtd_questions: list[str] = []
    for raw_line in block.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lower_line = line.casefold()
        if lower_line.startswith("decision gate reason:"):
            reason = line.split(":", 1)[1].strip() if ":" in line else ""
            decision_gate_reason = reason or None
            continue
        if not line.startswith("-"):
            continue
        item = line[1:].strip()
        lower_item = item.casefold()
        if lower_item.startswith(_DECISION_GATE_TAG):
            question = item[len(_DECISION_GATE_TAG) :].strip()
            if question:
                decision_gate_questions.append(question)
            continue
        if lower_item.startswith(_GTD_TAG):
            question = item[len(_GTD_TAG) :].strip()
            if question:
                gtd_questions.append(question)
    return LockedPrecheckQuestions(
        decision_gate_reason=decision_gate_reason,
        decision_gate_questions=_normalize_questions(decision_gate_questions),
        gtd_questions=_normalize_questions(gtd_questions),
    )


def remove_precheck_questions_block(*, current_description: str) -> str:
    current = str(current_description or "").strip()
    start_idx = current.find(PRECHECK_QUESTIONS_BLOCK_START)
    end_idx = current.find(PRECHECK_QUESTIONS_BLOCK_END, start_idx + len(PRECHECK_QUESTIONS_BLOCK_START))
    if start_idx < 0 or end_idx <= start_idx:
        return current
    end_marker_idx = end_idx + len(PRECHECK_QUESTIONS_BLOCK_END)
    prefix = current[:start_idx].rstrip()
    suffix = current[end_marker_idx:].lstrip()
    if prefix and suffix:
        return f"{prefix}\n\n{suffix}".strip()
    if prefix:
        return prefix
    return suffix


def upsert_precheck_questions_block(*, current_description: str, block: str) -> str:
    cleaned_block = str(block or "").strip()
    if not cleaned_block:
        return remove_precheck_questions_block(current_description=current_description)
    current = str(current_description or "").strip()
    if not current:
        return cleaned_block
    start_idx = current.find(PRECHECK_QUESTIONS_BLOCK_START)
    end_idx = current.find(PRECHECK_QUESTIONS_BLOCK_END, start_idx + len(PRECHECK_QUESTIONS_BLOCK_START))
    if start_idx >= 0 and end_idx > start_idx:
        end_marker_idx = end_idx + len(PRECHECK_QUESTIONS_BLOCK_END)
        prefix = current[:start_idx].rstrip()
        suffix = current[end_marker_idx:].lstrip()
        if prefix and suffix:
            return f"{prefix}\n\n{cleaned_block}\n\n{suffix}"
        if prefix:
            return f"{prefix}\n\n{cleaned_block}"
        if suffix:
            return f"{cleaned_block}\n\n{suffix}"
        return cleaned_block
    return f"{current}\n\n{cleaned_block}".strip()
