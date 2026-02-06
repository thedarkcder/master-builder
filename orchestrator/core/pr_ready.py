from __future__ import annotations

from dataclasses import dataclass

from orchestrator.tools.github_app import WorkflowCheckSuite


@dataclass(frozen=True)
class PrReadinessResult:
    ready: bool
    state: str
    reason: str
    missing_workflows: tuple[str, ...]
    pending_workflows: tuple[str, ...]
    failing_workflows: tuple[str, ...]


def evaluate_pr_readiness(
    *,
    review_summary_present: bool,
    required_workflows: tuple[str, ...],
    workflow_checks: list[WorkflowCheckSuite],
) -> PrReadinessResult:
    if not review_summary_present:
        return PrReadinessResult(
            ready=False,
            state="missing_review_summary",
            reason="Reviewer summary is missing",
            missing_workflows=(),
            pending_workflows=(),
            failing_workflows=(),
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
        )
    if failing:
        return PrReadinessResult(
            ready=False,
            state="failing_checks",
            reason="Required workflow checks are failing",
            missing_workflows=(),
            pending_workflows=tuple(pending),
            failing_workflows=tuple(failing),
        )
    if pending:
        return PrReadinessResult(
            ready=False,
            state="pending_checks",
            reason="Required workflow checks are still running",
            missing_workflows=(),
            pending_workflows=tuple(pending),
            failing_workflows=(),
        )

    return PrReadinessResult(
        ready=True,
        state="ready",
        reason="PR is ready for review",
        missing_workflows=(),
        pending_workflows=(),
        failing_workflows=(),
    )
