from __future__ import annotations

from dataclasses import asdict, dataclass, field
from collections.abc import Callable
from typing import Protocol

from orchestrator.core.followups import build_backlog_follow_up_draft
from orchestrator.core.workflow.runner_policies import (
    evaluate_placeholder_policy,
)

@dataclass(frozen=True)
class WorkflowRequest:
    tenant_id: str
    run_id: str
    issue_key: str
    issue_summary: str
    issue_description: str
    max_dev_test_review_loops: int
    suggested_test_commands: list[str] = field(default_factory=list)
    execution_repo_dir: str | None = None
    project_id: str | None = None


@dataclass(frozen=True)
class PmPlan:
    plan_steps: list[str]
    acceptance_criteria: list[str]
    risks: list[str]
    next_stage: str = "dev"


@dataclass(frozen=True)
class DevResult:
    change_summary: list[str]
    pr_url: str | None


@dataclass(frozen=True)
class TestResult:
    passed: bool
    guidance: list[str]
    feedback: str | None = None


# Prevent pytest from collecting this dataclass as a test class.
TestResult.__test__ = False


@dataclass(frozen=True)
class ReviewResult:
    approved: bool
    summary: list[str]
    feedback: str | None = None
    pr_url: str | None = None


@dataclass(frozen=True)
class WorkflowDiagnostics:
    stage: str
    message: str
    attempts: int
    history: list[dict[str, str]]


@dataclass(frozen=True)
class WorkflowResult:
    succeeded: bool
    plan: PmPlan | None
    pr_url: str | None
    summary: list[str]
    test_guidance: list[str]
    attempts: int
    follow_up_issue: dict | None = None
    diagnostics: WorkflowDiagnostics | None = None

    def to_plan_payload(self) -> dict:
        payload = {
            "attempts": self.attempts,
            "succeeded": self.succeeded,
            "summary": self.summary,
            "test_guidance": self.test_guidance,
            "pr_url": self.pr_url,
            "follow_up_issue": self.follow_up_issue,
        }
        if self.plan is not None:
            payload["plan"] = asdict(self.plan)
        if self.diagnostics is not None:
            payload["diagnostics"] = asdict(self.diagnostics)
        return payload


class WorkflowAgents(Protocol):
    def pm(
        self,
        request: WorkflowRequest,
        attempt: int,
        feedback: str | None,
        history: list[dict[str, str]],
        last_dev_result: DevResult | None,
        last_test_result: TestResult | None,
        last_review_result: ReviewResult | None,
    ) -> PmPlan:
        ...

    def dev(
        self,
        request: WorkflowRequest,
        plan: PmPlan,
        attempt: int,
        feedback: str | None,
    ) -> DevResult:
        ...

    def test(
        self,
        request: WorkflowRequest,
        plan: PmPlan,
        dev_result: DevResult,
        attempt: int,
    ) -> TestResult:
        ...

    def review(
        self,
        request: WorkflowRequest,
        plan: PmPlan,
        dev_result: DevResult,
        test_result: TestResult,
        attempt: int,
    ) -> ReviewResult:
        ...


class WorkflowRunner:
    def __init__(
        self,
        agents: WorkflowAgents,
    ):
        self._agents = agents

    def run(
        self,
        request: WorkflowRequest,
        *,
        test_feedback_hook: Callable[[int, str], None] | None = None,
    ) -> WorkflowResult:
        history: list[dict[str, str]] = []
        max_attempts = max(1, request.max_dev_test_review_loops)

        feedback: str | None = None
        last_dev_result: DevResult | None = None
        last_test_result: TestResult | None = None
        last_review_result: ReviewResult | None = None
        plan: PmPlan | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                plan = self._agents.pm(
                    request,
                    attempt,
                    feedback,
                    history,
                    last_dev_result,
                    last_test_result,
                    last_review_result,
                )
            except Exception as exc:  # pragma: no cover - exercised via tests
                return self._failure(
                    plan=plan,
                    stage="pm",
                    message=f"PM stage failed: {exc}",
                    attempts=attempt - 1,
                    history=history,
                    request=request,
                )
            pm_blocker = _extract_list_blocker(
                [*plan.plan_steps, *plan.acceptance_criteria, *plan.risks]
            )
            if pm_blocker is not None:
                history.append({"stage": "pm", "attempt": str(attempt), "event": pm_blocker})
                return self._failure(
                    plan=plan,
                    stage="pm",
                    message=f"PM stage blocked: {pm_blocker}",
                    attempts=attempt - 1,
                    history=history,
                    request=request,
                )

            desired_next_stage = str(plan.next_stage or "dev").strip().lower()
            route_to_test = desired_next_stage == "test" and last_dev_result is not None
            if desired_next_stage not in {"dev", "test"}:
                route_to_test = False

            dev_result: DevResult
            if route_to_test:
                dev_result = last_dev_result
                history.append({"stage": "pm", "attempt": str(attempt), "event": "route:test"})
            else:
                history.append({"stage": "pm", "attempt": str(attempt), "event": "route:dev"})
                feedback_for_dev = feedback
                feedback = None
            try:
                if not route_to_test:
                    dev_result = self._agents.dev(request, plan, attempt, feedback_for_dev)
                    dev_blocker = _extract_dev_blocker(dev_result)
                    if dev_blocker is not None:
                        history.append({"stage": "dev", "attempt": str(attempt), "event": dev_blocker})
                        return self._failure(
                            plan=plan,
                            stage="dev",
                            message=f"Dev stage blocked: {dev_blocker}",
                            attempts=attempt,
                            history=history,
                            request=request,
                        )
                    last_dev_result = dev_result
            except Exception as exc:  # pragma: no cover - exercised via tests
                history.append(
                    {"stage": "dev", "attempt": str(attempt), "event": f"exception:{exc}"}
                )
                return self._failure(
                    plan=plan,
                    stage="dev",
                    message=f"Dev stage failed: {exc}",
                    attempts=attempt,
                    history=history,
                    request=request,
                )

            try:
                test_result = self._agents.test(request, plan, dev_result, attempt)
            except Exception as exc:  # pragma: no cover - exercised via tests
                history.append(
                    {"stage": "test", "attempt": str(attempt), "event": f"exception:{exc}"}
                )
                return self._failure(
                    plan=plan,
                    stage="test",
                    message=f"Test stage failed: {exc}",
                    attempts=attempt,
                    history=history,
                    request=request,
                )
            last_test_result = test_result
            test_blocker = _extract_first_blocker([test_result.feedback, *test_result.guidance])
            if test_blocker is not None:
                history.append({"stage": "test", "attempt": str(attempt), "event": test_blocker})
                if test_feedback_hook is not None:
                    test_feedback_hook(attempt, test_blocker)
                feedback = test_blocker
                if attempt >= max_attempts:
                    return self._failure(
                        plan=plan,
                        stage="test",
                        message="Max workflow attempts reached after test failures",
                        attempts=attempt,
                        history=history,
                        request=request,
                    )
                continue

            if not test_result.passed:
                feedback = test_result.feedback or "Tests failed with no feedback"
                history.append({"stage": "test", "attempt": str(attempt), "event": feedback})
                if test_feedback_hook is not None:
                    test_feedback_hook(attempt, feedback)
                if attempt >= max_attempts:
                    return self._failure(
                        plan=plan,
                        stage="test",
                        message="Max workflow attempts reached after test failures",
                        attempts=attempt,
                        history=history,
                        request=request,
                    )
                continue

            try:
                review_result = self._agents.review(
                    request,
                    plan,
                    dev_result,
                    test_result,
                    attempt,
                )
            except Exception as exc:  # pragma: no cover - exercised via tests
                history.append(
                    {"stage": "review", "attempt": str(attempt), "event": f"exception:{exc}"}
                )
                return self._failure(
                    plan=plan,
                    stage="review",
                    message=f"Review stage failed: {exc}",
                    attempts=attempt,
                    history=history,
                    request=request,
                )
            last_review_result = review_result
            review_blocker = _extract_first_blocker([review_result.feedback, *review_result.summary])
            if review_blocker is not None:
                history.append({"stage": "review", "attempt": str(attempt), "event": review_blocker})
                return self._failure(
                    plan=plan,
                    stage="review",
                    message=f"Review stage blocked: {review_blocker}",
                    attempts=attempt,
                    history=history,
                    request=request,
                )

            if not review_result.approved:
                feedback = review_result.feedback or "Review requested changes"
                history.append({"stage": "review", "attempt": str(attempt), "event": feedback})
                if attempt >= max_attempts:
                    return self._failure(
                        plan=plan,
                        stage="review",
                        message="Max workflow attempts reached after review feedback",
                        attempts=attempt,
                        history=history,
                        request=request,
                    )
                continue

            pr_url = dev_result.pr_url or review_result.pr_url
            if not pr_url:
                history.append(
                    {
                        "stage": "review",
                        "attempt": str(attempt),
                        "event": "missing_pr_url",
                    }
                )
                return self._failure(
                    plan=plan,
                    stage="review",
                    message="Workflow succeeded but no PR URL was produced",
                    attempts=attempt,
                    history=history,
                    request=request,
                )

            placeholder_failure = self._placeholder_policy_failure(
                request=request,
                dev_result=dev_result,
                test_result=test_result,
                review_result=review_result,
                pr_url=pr_url,
                attempts=attempt,
                history=history,
            )
            if placeholder_failure is not None:
                return placeholder_failure

            return WorkflowResult(
                succeeded=True,
                plan=plan,
                pr_url=pr_url,
                summary=review_result.summary or dev_result.change_summary,
                test_guidance=test_result.guidance,
                attempts=attempt,
            )

        return self._failure(
            plan=plan,
            stage="runner",
            message="Workflow stopped before producing a terminal result",
            attempts=max_attempts,
            history=history,
            request=request,
        )

    def _failure(
        self,
        *,
        plan: PmPlan | None,
        stage: str,
        message: str,
        attempts: int,
        history: list[dict[str, str]],
        request: WorkflowRequest | None,
        follow_up_issue: dict | None = None,
        skip_auto_follow_up: bool = False,
    ) -> WorkflowResult:
        if follow_up_issue is None and request is not None and not skip_auto_follow_up:
            draft = build_backlog_follow_up_draft(
                title=f"Follow-up for {request.issue_key}: {stage} handling",
                why_it_matters=message,
                impact=(
                    f"Workflow run {request.run_id} for {request.issue_key} ended in stage '{stage}'"
                ),
                suggested_approach=(
                    "Address the reported stage failure and rerun from To Do after validation."
                ),
                origin_issue_key=request.issue_key,
            )
            follow_up_issue = draft.to_payload()
        return WorkflowResult(
            succeeded=False,
            plan=plan,
            pr_url=None,
            summary=[],
            test_guidance=[],
            attempts=attempts,
            follow_up_issue=follow_up_issue,
            diagnostics=WorkflowDiagnostics(
                stage=stage,
                message=message,
                attempts=attempts,
                history=history,
            ),
        )

    def _placeholder_policy_failure(
        self,
        *,
        request: WorkflowRequest,
        dev_result: DevResult,
        test_result: TestResult,
        review_result: ReviewResult,
        pr_url: str,
        attempts: int,
        history: list[dict[str, str]],
    ) -> WorkflowResult | None:
        return evaluate_placeholder_policy(
            request=request,
            dev_result=dev_result,
            test_result=test_result,
            review_result=review_result,
            pr_url=pr_url,
            attempts=attempts,
            history=history,
            failure_factory=self._failure,
        )

def _extract_dev_blocker(dev_result: DevResult) -> str | None:
    return _extract_list_blocker(dev_result.change_summary)


def _extract_list_blocker(entries: list[str]) -> str | None:
    return _extract_first_blocker(entries)


def _extract_first_blocker(entries: list[str | None]) -> str | None:
    for entry in entries:
        text = str(entry or "").strip()
        if text.lower().startswith("blocked:"):
            return text
    return None
