from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass


def _compact_text(value: object) -> str:
    return " ".join(str(value or "").split())


def _first_text(mapping: Mapping[str, object], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        candidate = _compact_text(mapping.get(key))
        if candidate:
            return candidate
    return None


@dataclass(frozen=True)
class ClarificationQuestion:
    question: str
    why_it_matters: str | None = None
    question_id: str | None = None
    source_ref: str | None = None

    def __post_init__(self) -> None:
        normalized_question = _compact_text(self.question)
        if not normalized_question:
            raise ValueError("Clarification question text is required")
        object.__setattr__(self, "question", normalized_question)
        object.__setattr__(
            self, "why_it_matters", _compact_text(self.why_it_matters) or None
        )
        object.__setattr__(self, "question_id", _compact_text(self.question_id) or None)
        object.__setattr__(self, "source_ref", _compact_text(self.source_ref) or None)

    def to_payload(self) -> dict[str, str]:
        payload = {"question": self.question}
        if self.why_it_matters:
            payload["why_it_matters"] = self.why_it_matters
        if self.question_id:
            payload["id"] = self.question_id
        if self.source_ref:
            payload["source_ref"] = self.source_ref
        return payload

    @classmethod
    def parse(cls, value: object) -> ClarificationQuestion | None:
        if isinstance(value, ClarificationQuestion):
            return value
        if isinstance(value, Mapping):
            question = _first_text(
                value,
                (
                    "question",
                    "stakeholder_question",
                    "question_text",
                    "original_question",
                ),
            )
            if not question:
                return None
            return cls(
                question=question,
                why_it_matters=_first_text(value, ("why_it_matters", "reason", "note")),
                question_id=_first_text(value, ("id", "question_id")),
                source_ref=_first_text(value, ("source_ref", "source_child_key")),
            )
        normalized_question = _compact_text(value)
        if not normalized_question:
            return None
        return cls(question=normalized_question)


@dataclass(frozen=True)
class ClarificationQuestionSet:
    questions: tuple[ClarificationQuestion, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.questions)

    @classmethod
    def from_values(cls, values: Iterable[object] | None) -> ClarificationQuestionSet:
        ordered: list[ClarificationQuestion] = []
        seen: set[tuple[str, str | None, str | None, str | None]] = set()
        for value in values or ():
            question = ClarificationQuestion.parse(value)
            if question is None:
                continue
            dedupe_key = (
                question.question,
                question.why_it_matters,
                question.question_id,
                question.source_ref,
            )
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            ordered.append(question)
        return cls(questions=tuple(ordered))

    @property
    def prompts(self) -> tuple[str, ...]:
        return tuple(question.question for question in self.questions)

    def to_payload(self) -> list[dict[str, str]]:
        return [question.to_payload() for question in self.questions]

    def render_lines(
        self,
        *,
        numbered: bool = False,
        include_reasons: bool = False,
        limit: int | None = None,
    ) -> tuple[str, ...]:
        lines: list[str] = []
        selected = self.questions[:limit] if limit is not None else self.questions
        for index, question in enumerate(selected, start=1):
            prefix = f"{index}. " if numbered else "- "
            lines.append(f"{prefix}{question.question}")
            if include_reasons and question.why_it_matters:
                lines.append(f"   Why it matters: {question.why_it_matters}")
        return tuple(lines)
