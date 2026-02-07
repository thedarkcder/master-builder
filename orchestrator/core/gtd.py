from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GoodToDoValidationResult:
    valid: bool
    missing_criteria: tuple[str, ...]
    clarification_questions: tuple[str, ...]


def validate_good_to_do(*, issue_summary: str, issue_description: str) -> GoodToDoValidationResult:
    text = f"{issue_summary}\n{issue_description}".lower()

    checks: tuple[tuple[str, tuple[str, ...], str], ...] = (
        (
            "objective",
            ("objective", "outcome"),
            "What objective should this run achieve in one sentence?",
        ),
        (
            "scope",
            ("scope", "in scope", "out of scope"),
            "What is explicitly in scope and out of scope for this issue?",
        ),
        (
            "acceptance criteria",
            ("acceptance criteria", "acceptance"),
            "Which concrete acceptance criteria define done?",
        ),
        (
            "context",
            ("context", "component", "repo", "link"),
            "What repo/component context should the run assume?",
        ),
        (
            "testability",
            ("how to test", "test", "validation"),
            "How should the resulting change be validated?",
        ),
        (
            "nfr intent",
            ("mvp", "scale-ready", "non-functional", "nfr"),
            "Should this be MVP-quick or scale-ready for NFRs?",
        ),
        (
            "risks/dependencies",
            ("risk", "dependency", "blocked"),
            "What key risks or dependencies must be accounted for?",
        ),
    )

    missing: list[str] = []
    questions: list[str] = []

    for criterion, signals, question in checks:
        if any(signal in text for signal in signals):
            continue
        missing.append(criterion)
        questions.append(question)

    return GoodToDoValidationResult(
        valid=not missing,
        missing_criteria=tuple(missing),
        clarification_questions=tuple(questions),
    )
