from __future__ import annotations

from dataclasses import dataclass
import re

from orchestrator.tools.github_app import WorkflowCheckSuite


@dataclass(frozen=True)
class PrReadinessResult:
    ready: bool
    state: str
    reason: str
    missing_workflows: tuple[str, ...]
    pending_workflows: tuple[str, ...]
    failing_workflows: tuple[str, ...]
    missing_review_sections: tuple[str, ...] = ()


_REVIEW_SECTION_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "good": (
        re.compile(r"(?im)^\s{0,3}(?:#+\s*)?good\s*:"),
        re.compile(r"(?im)^\s{0,3}(?:#+\s*)?what changed\s*:"),
    ),
    "risks": (
        re.compile(r"(?im)^\s{0,3}(?:#+\s*)?risks?\s*:"),
        re.compile(r"(?im)^\s{0,3}(?:#+\s*)?risk\s*/\s*impact\s*:"),
    ),
    "must-fix": (
        re.compile(r"(?im)^\s{0,3}(?:#+\s*)?must[- ]fix(?: findings)?\s*:"),
        re.compile(r"(?im)^\s{0,3}(?:#+\s*)?must[- ]fix\s*:"),
    ),
    "tests": (
        re.compile(r"(?im)^\s{0,3}(?:#+\s*)?tests?\s*:"),
        re.compile(r"(?im)^\s{0,3}(?:#+\s*)?how to test\s*:"),
    ),
    "questions": (re.compile(r"(?im)^\s{0,3}(?:#+\s*)?questions?\s*:") ,),
    "follow-ups": (
        re.compile(r"(?im)^\s{0,3}(?:#+\s*)?follow[- ]ups?\s*:"),
        re.compile(r"(?im)^\s{0,3}(?:#+\s*)?followups\s*:"),
    ),
}


def _missing_review_sections(review_summary_markdown: str) -> tuple[str, ...]:
    missing: list[str] = []
    for section_name, patterns in _REVIEW_SECTION_PATTERNS.items():
        if any(pattern.search(review_summary_markdown) for pattern in patterns):
            continue
        missing.append(section_name)
    return tuple(missing)


def evaluate_pr_readiness(
    *,
    review_summary_markdown: str | None,
    required_workflows: tuple[str, ...],
    workflow_checks: list[WorkflowCheckSuite],
) -> PrReadinessResult:
    normalized_review_summary = (review_summary_markdown or "").strip()
    if not normalized_review_summary:
        return PrReadinessResult(
            ready=False,
            state="missing_review_summary",
            reason="Reviewer summary is missing",
            missing_workflows=(),
            pending_workflows=(),
            failing_workflows=(),
        )
    missing_sections = _missing_review_sections(normalized_review_summary)
    if missing_sections:
        return PrReadinessResult(
            ready=False,
            state="missing_review_sections",
            reason="Reviewer summary is missing required sections",
            missing_workflows=(),
            pending_workflows=(),
            failing_workflows=(),
            missing_review_sections=missing_sections,
        )

    checks_by_name = {item.name.lower(): item for item in workflow_checks}
    missing: list[str] = []
    pending: list[str] = []
    failing: list[str] = []

    for workflow_name in required_workflows:
        check = checks_by_name.get(workflow_name.lower())
        if check is None:
            missing.append(workflow_name)
            continue

        if check.status != "completed":
            pending.append(workflow_name)
            continue

        if check.conclusion not in {"success", "neutral", "skipped"}:
            failing.append(workflow_name)

    if missing:
        return PrReadinessResult(
            ready=False,
            state="missing_checks",
            reason="Required workflow checks are missing",
            missing_workflows=tuple(missing),
            pending_workflows=tuple(pending),
            failing_workflows=tuple(failing),
            missing_review_sections=(),
        )
    if failing:
        return PrReadinessResult(
            ready=False,
            state="failing_checks",
            reason="Required workflow checks are failing",
            missing_workflows=(),
            pending_workflows=tuple(pending),
            failing_workflows=tuple(failing),
            missing_review_sections=(),
        )
    if pending:
        return PrReadinessResult(
            ready=False,
            state="pending_checks",
            reason="Required workflow checks are still running",
            missing_workflows=(),
            pending_workflows=tuple(pending),
            failing_workflows=(),
            missing_review_sections=(),
        )

    return PrReadinessResult(
        ready=True,
        state="ready",
        reason="PR is ready for review",
        missing_workflows=(),
        pending_workflows=(),
        failing_workflows=(),
        missing_review_sections=(),
    )
