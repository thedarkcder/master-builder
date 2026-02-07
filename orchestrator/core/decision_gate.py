from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

RULES_FILE_PATH = Path(".codex/rules/decision_gate.md")


@dataclass(frozen=True)
class DecisionGateResult:
    triggered: bool
    reason: str
    missing_sections: tuple[str, ...]
    questions: tuple[str, ...]
    recommendation: str
    tags: tuple[str, ...]

    def to_payload(self) -> dict[str, object]:
        return {
            "triggered": self.triggered,
            "reason": self.reason,
            "missing_sections": list(self.missing_sections),
            "questions": list(self.questions),
            "recommendation": self.recommendation,
            "tags": list(self.tags),
            "signal_summary": format_decision_gate_summary(self),
        }


@dataclass(frozen=True)
class DecisionGateRules:
    required_sections: tuple[str, ...]
    nfr_markers: tuple[str, ...]
    ambiguity_markers: tuple[str, ...]
    questions: tuple[str, ...]
    tags: tuple[str, ...]
    clear_reason: str
    blocked_recommendation: str
    clear_summary: str
    blocked_title: str
    missing_sections_prefix: str
    ambiguity_prefix: str
    options_line: str


def _normalize_heading(raw_heading: str) -> str:
    return raw_heading.strip().lower()


def _parse_markdown_rule_file(markdown: str) -> tuple[dict[str, list[str]], dict[str, str]]:
    current_section: str | None = None
    list_sections: dict[str, list[str]] = {}
    message_map: dict[str, str] = {}

    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("## "):
            current_section = _normalize_heading(line[3:])
            list_sections.setdefault(current_section, [])
            continue
        if not line.startswith("- ") or current_section is None:
            continue
        item = line[2:].strip()
        if current_section == "messages":
            key, sep, value = item.partition(":")
            if sep and key.strip() and value.strip():
                message_map[key.strip()] = value.strip()
            continue
        list_sections[current_section].append(item)

    return list_sections, message_map


def _required_non_empty(section_values: dict[str, list[str]], *, key: str) -> tuple[str, ...]:
    values = tuple(v for v in section_values.get(key, ()) if v)
    if not values:
        raise ValueError(f"Decision Gate rules missing required section values: '{key}'")
    return values


def _required_message(messages: dict[str, str], *, key: str) -> str:
    value = messages.get(key, "").strip()
    if not value:
        raise ValueError(f"Decision Gate rules missing required message key: '{key}'")
    return value


@lru_cache(maxsize=4)
def _load_decision_gate_rules_cached(path_text: str) -> DecisionGateRules:
    path = Path(path_text)
    if not path.exists():
        raise FileNotFoundError(f"Decision Gate rules file not found: {path}")

    list_sections, messages = _parse_markdown_rule_file(path.read_text(encoding="utf-8"))
    return DecisionGateRules(
        required_sections=_required_non_empty(list_sections, key="required sections"),
        nfr_markers=_required_non_empty(list_sections, key="nfr markers"),
        ambiguity_markers=_required_non_empty(list_sections, key="ambiguity markers"),
        questions=_required_non_empty(list_sections, key="resolution questions"),
        tags=_required_non_empty(list_sections, key="tags"),
        clear_reason=_required_message(messages, key="clear_reason"),
        blocked_recommendation=_required_message(messages, key="blocked_recommendation"),
        clear_summary=_required_message(messages, key="clear_summary"),
        blocked_title=_required_message(messages, key="blocked_title"),
        missing_sections_prefix=_required_message(messages, key="missing_sections_prefix"),
        ambiguity_prefix=_required_message(messages, key="ambiguity_prefix"),
        options_line=_required_message(messages, key="options_line"),
    )


def reset_decision_gate_rules_cache() -> None:
    _load_decision_gate_rules_cached.cache_clear()


def load_decision_gate_rules(*, path: str | Path | None = None) -> DecisionGateRules:
    rules_path = Path(path) if path is not None else RULES_FILE_PATH
    return _load_decision_gate_rules_cached(str(rules_path))


def _has_section(description: str, section: str) -> bool:
    return section.lower() in description.lower()


def evaluate_decision_gate(
    *,
    issue_summary: str | None,
    issue_description: str | None,
    rules_path: str | Path | None = None,
) -> DecisionGateResult:
    rules = load_decision_gate_rules(path=rules_path)
    summary = (issue_summary or "").strip()
    description = (issue_description or "").strip()
    normalized = f"{summary}\n{description}".lower()

    missing_sections: list[str] = []
    for section in rules.required_sections:
        if not _has_section(description, section):
            missing_sections.append(section)

    if not any(marker.lower() in normalized for marker in rules.nfr_markers):
        missing_sections.append("NFR intent (MVP vs scale-ready)")

    ambiguous_markers = [marker for marker in rules.ambiguity_markers if marker.lower() in normalized]

    if not missing_sections and not ambiguous_markers:
        return DecisionGateResult(
            triggered=False,
            reason=rules.clear_reason,
            missing_sections=(),
            questions=(),
            recommendation="Proceed",
            tags=rules.tags,
        )

    reason_parts: list[str] = []
    if missing_sections:
        reason_parts.append(f"{rules.missing_sections_prefix}: {', '.join(missing_sections)}")
    if ambiguous_markers:
        reason_parts.append(f"{rules.ambiguity_prefix}: {', '.join(ambiguous_markers)}")

    return DecisionGateResult(
        triggered=True,
        reason="; ".join(reason_parts),
        missing_sections=tuple(missing_sections),
        questions=rules.questions,
        recommendation=rules.blocked_recommendation,
        tags=rules.tags,
    )


def format_decision_gate_summary(result: DecisionGateResult) -> str:
    rules = load_decision_gate_rules()
    if not result.triggered:
        return rules.clear_summary

    lines = [
        rules.blocked_title,
        f"Reason: {result.reason}",
        rules.options_line,
        f"Recommendation: {result.recommendation}",
        "Questions:",
    ]
    lines.extend(f"{idx}) {question}" for idx, question in enumerate(result.questions[:5], start=1))
    lines.append("Tags: " + " ".join(result.tags))
    return "\n".join(lines)
